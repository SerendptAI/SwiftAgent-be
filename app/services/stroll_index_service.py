"""
Stroll Navigation Report Service — builds a full-context navigation report
from stroll data for LLM-driven how-to guides.

The LLM receives this report as a tool result, reasons over it, and returns
ordered page_ids + instructions. The caller then reconstructs the navigation
guide (screenshots + highlights) from the page_lookup dict.
"""

import json
import logging
import re
from typing import Optional

from app.models.stroll_models import (
    BoundingBox,
    NavigationStep,
    FindFeatureResult,
    StrollVersion,
)
from app.services.stroll_service import get_latest_version

logger = logging.getLogger(__name__)


async def generate_navigation_report(company_id: str) -> Optional[dict]:
    """
    Build a full navigation report from the latest stroll version.

    Returns a dict with:
    - "report": markdown text describing all pages, elements, and navigation edges
    - "page_lookup": dict mapping page_id → {title, url, screenshot_url, elements: [{selector, label, bbox}]}
    - "version": the StrollVersion object

    Returns None if no stroll data exists for the company.
    """
    version = await get_latest_version(company_id)
    if not version or not version.graph.nodes:
        return None

    graph = version.graph
    page_lookup: dict[str, dict] = {}

    # --- build markdown report ---
    lines = [
        "# Dashboard Navigation Report",
        "",
    ]

    for node_id, node in graph.nodes.items():
        screenshot_url = version.screenshot_urls.get(node_id, "")

        lines.append(f"## Page: {node.title}")
        lines.append(f"- **Page ID:** {node_id}")
        lines.append(f"- **URL:** {node.url}")
        lines.append(f"- **Summary:** {node.page_summary}")
        lines.append(f"- **Screenshot:** {screenshot_url}")

        # list interactive elements
        if node.elements:
            lines.append("- **Interactive Elements:**")
            for elem in node.elements:
                desc = elem.human_description or elem.label or elem.selector

                # find where this element navigates to (if nav type)
                dest = ""
                if elem.type == "nav":
                    for edge in graph.edges:
                        if edge.from_page == node_id and edge.via.selector == elem.selector:
                            dest_node = graph.nodes.get(edge.to_page)
                            if dest_node:
                                dest = f" → navigates to **{dest_node.title}** (page_id: {edge.to_page})"
                            break

                bbox_str = ""
                if elem.bbox:
                    bbox_str = f" [bbox: x={elem.bbox.x}, y={elem.bbox.y}, w={elem.bbox.w}, h={elem.bbox.h}]"

                lines.append(f"  - `{elem.selector}` — \"{elem.label}\" — {desc}{dest}{bbox_str}")

        lines.append("")

        # build lookup entry for reconstruction
        page_lookup[node_id] = {
            "title": node.title,
            "url": node.url,
            "screenshot_url": screenshot_url,
            "elements": [
                {
                    "selector": elem.selector,
                    "label": elem.label,
                    "human_description": elem.human_description,
                    "bbox": elem.bbox.model_dump() if elem.bbox else None,
                }
                for elem in node.elements
            ],
        }

    # navigation edges section
    lines.append("## Navigation Edges")
    lines.append("")
    for edge in graph.edges:
        from_title = graph.nodes[edge.from_page].title if edge.from_page in graph.nodes else edge.from_page
        to_title = graph.nodes[edge.to_page].title if edge.to_page in graph.nodes else edge.to_page
        via_label = edge.via.label or edge.via.human_description or edge.via.selector
        lines.append(f"- {from_title} → {to_title} (click \"{via_label}\")")

    lines.append("")

    report_text = "\n".join(lines)

    return {
        "report": report_text,
        "page_lookup": page_lookup,
        "version": version,
    }


async def get_all_navigation_steps(company_id: str) -> Optional[FindFeatureResult]:
    """
    Traverse the latest NavGraph and extract a flattened sequence of navigation steps 
    for the entire dashboard using BFS.
    """
    version = await get_latest_version(company_id)
    if not version or not version.graph.nodes:
        return None

    graph = version.graph
    root_id = graph.get_root_id()
    if not root_id:
        return None

    # We need a page_lookup just like generate_navigation_report does
    # to reconstruct the full guide easily. We can just build it here.
    page_lookup: dict[str, dict] = {}
    for node_id, node in graph.nodes.items():
        page_lookup[node_id] = {
            "title": node.title,
            "url": node.url,
            "screenshot_url": version.screenshot_urls.get(node_id, ""),
            "elements": [
                {
                    "selector": elem.selector,
                    "label": elem.label,
                    "human_description": elem.human_description,
                    "bbox": elem.bbox.model_dump() if elem.bbox else None,
                }
                for elem in node.elements
            ],
            "page_summary": node.page_summary,
        }

    # BFS traversal to order the pages logically
    from collections import deque
    queue = deque([root_id])
    visited = {root_id}

    navigation_steps_json = []

    # First step: Dashboard root
    navigation_steps_json.append({
        "page_id": root_id,
        "instruction": "Start at the " + graph.nodes[root_id].title + ". " + graph.nodes[root_id].page_summary,
    })

    while queue:
        current_id = queue.popleft()
        
        # Find all outward edges from current node
        outward_edges = [e for e in graph.edges if e.from_page == current_id]
        for edge in outward_edges:
            dest_id = edge.to_page
            if dest_id not in visited and dest_id in graph.nodes:
                visited.add(dest_id)
                queue.append(dest_id)
                
                # Add step to navigate to this child
                navigation_steps_json.append({
                    "page_id": current_id,
                    "instruction": edge.instruction,
                    "element_selector": edge.via.selector,
                })
                # Add step describing the child page
                navigation_steps_json.append({
                    "page_id": dest_id,
                    "instruction": f"You are now on the {graph.nodes[dest_id].title}. {graph.nodes[dest_id].page_summary}",
                })

    return reconstruct_navigation_guide(navigation_steps_json, page_lookup)


def reconstruct_navigation_guide(
    navigation_steps_json: list[dict],
    page_lookup: dict[str, dict],
) -> Optional[FindFeatureResult]:
    """
    Reconstruct a FindFeatureResult from the LLM's ordered navigation steps.

    Args:
        navigation_steps_json: list of dicts from LLM, each with:
            - page_id: str
            - instruction: str
            - element_selector: str (optional, for highlight bbox)
        page_lookup: dict from generate_navigation_report()

    Returns FindFeatureResult with steps + path_summary, or None if invalid.
    """
    if not navigation_steps_json:
        return None

    steps: list[NavigationStep] = []
    path_summary: list[str] = []

    for i, step_data in enumerate(navigation_steps_json):
        page_id = step_data.get("page_id", "")
        instruction = step_data.get("instruction", "")
        element_selector = step_data.get("element_selector")

        page_info = page_lookup.get(page_id)
        if not page_info:
            continue

        screenshot_url = page_info.get("screenshot_url", "")
        page_title = page_info.get("title", "")
        path_summary.append(page_title)

        # find highlight bbox if element_selector provided
        highlight = None
        if element_selector:
            for elem in page_info.get("elements", []):
                if elem["selector"] == element_selector:
                    if elem.get("bbox"):
                        highlight = BoundingBox(**elem["bbox"])
                    break

        steps.append(NavigationStep(
            step=i + 1,
            page_title=page_title,
            instruction=instruction,
            screenshot_url=screenshot_url,
            highlight=highlight,
        ))

    if not steps:
        return None

    return FindFeatureResult(path_summary=path_summary, steps=steps)


def extract_navigation_steps(reply_text: str) -> tuple[list[dict], str]:
    """
    Extract the navigation_steps JSON block from the LLM's reply text.

    Returns:
        (navigation_steps, cleaned_reply) — the parsed steps list and the
        reply text with the JSON block removed.
    """
    # look for ```navigation_steps ... ``` or ```json ... ``` blocks containing navigation_steps
    patterns = [
        r"```navigation_steps\s*\n(.*?)```",
        r"```json\s*\n(\{[^`]*\"navigation_steps\"[^`]*\})```",
        r"```json\s*\n(\[[^`]*\])```",
        r"```\s*\n(\{[^`]*\"navigation_steps\"[^`]*\})```",
    ]

    for pattern in patterns:
        match = re.search(pattern, reply_text, re.DOTALL)
        if match:
            try:
                raw = match.group(1).strip()
                parsed = json.loads(raw)

                # handle both {"navigation_steps": [...]} and [...]
                if isinstance(parsed, dict):
                    steps = parsed.get("navigation_steps", [])
                elif isinstance(parsed, list):
                    steps = parsed
                else:
                    continue

                # remove the JSON block from the reply
                cleaned = reply_text[:match.start()] + reply_text[match.end():]
                cleaned = cleaned.strip()

                return steps, cleaned
            except (json.JSONDecodeError, KeyError):
                continue

    return [], reply_text
