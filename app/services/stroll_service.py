"""
Stroll Service — BFS dashboard crawler with Playwright + Claude vision.

Core responsibilities:
- Headless browser automation (Playwright) to crawl customer dashboards
- Claude vision analysis for semantic page understanding
- Screenshot capture + upload to Cloudinary
- Navigation graph construction
- Version diffing and commit to MongoDB
"""

import base64
import hashlib
import json
import logging
from collections import deque
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional
from urllib.parse import urljoin, urlparse
from uuid import uuid4

import anthropic
from playwright.async_api import async_playwright, Browser, Page

from app.core.config import settings
from app.core.database import db
from app.models.stroll_models import (
    BoundingBox,
    DiffLog,
    Edge,
    InteractiveElement,
    MovedFeature,
    NavGraph,
    PageNode,
    StrollConfig,
    StrollConfigCreate,
    StrollVersion,
)
from app.services.cloudinary_service import upload_document

logger = logging.getLogger(__name__)

# shared Playwright browser instance (initialized in lifespan)
_browser: Optional[Browser] = None


async def init_browser():
    """Initialize Playwright browser. Call once at app startup."""
    global _browser
    pw = await async_playwright().start()
    _browser = await pw.chromium.launch(headless=True)
    logger.info("Playwright browser initialized")


async def close_browser():
    """Close Playwright browser. Call at app shutdown."""
    global _browser
    if _browser:
        await _browser.close()
        _browser = None
        logger.info("Playwright browser closed")


def _get_anthropic_client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)


async def _analyze_page_with_vision(
    screenshot_bytes: bytes,
    raw_elements: list[dict],
    page_title: str,
    page_url: str,
) -> dict:
    """
    Use Claude vision to semantically understand a page screenshot.
    Returns enriched element descriptions and a page summary.
    """
    client = _get_anthropic_client()

    elements_text = json.dumps(raw_elements[:30], indent=2)  # cap to avoid token overflow

    try:
        response = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": base64.b64encode(screenshot_bytes).decode(),
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            f"This is a screenshot of the page '{page_title}' at {page_url}.\n\n"
                            f"Detected interactive elements from the DOM:\n{elements_text}\n\n"
                            "Please provide:\n"
                            "1. A one-sentence summary of what this page is for.\n"
                            "2. For each interactive element above, a human-readable description "
                            "of what it does (e.g., 'Opens the billing settings page'). "
                            "If an element has no text label (icon-only), describe it based on "
                            "what you see in the screenshot.\n\n"
                            "Respond in JSON format:\n"
                            '{"page_summary": "...", "elements": [{"selector": "...", '
                            '"human_description": "..."}]}'
                        ),
                    },
                ],
            }],
        )

        text = response.content[0].text
        # extract JSON from response (handle markdown code blocks)
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]

        return json.loads(text.strip())

    except Exception as e:
        logger.warning(f"Vision analysis failed for {page_url}: {e}")
        return {"page_summary": page_title, "elements": []}

DETECT_ELEMENTS_JS = """
() => {
    const elements = [];
    const selectors = 'a[href], button, [role="tab"], [role="menuitem"], ' +
                      '[role="button"], nav a, .nav-link, .sidebar-link, ' +
                      '[data-toggle], [data-bs-toggle]';
    const nodes = document.querySelectorAll(selectors);

    nodes.forEach((el, i) => {
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) return;  // skip hidden elements

        const label = (
            el.textContent?.trim() ||
            el.getAttribute('aria-label') ||
            el.getAttribute('title') ||
            el.getAttribute('placeholder') ||
            ''
        ).slice(0, 100);

        const href = el.getAttribute('href') || '';
        const isNav = el.tagName === 'A' || el.closest('nav') !== null ||
                      el.getAttribute('role') === 'tab' ||
                      el.getAttribute('role') === 'menuitem';

        // build a reasonably unique CSS selector
        let selector = '';
        if (el.id) {
            selector = '#' + el.id;
        } else {
            const tag = el.tagName.toLowerCase();
            const classes = Array.from(el.classList).slice(0, 3).join('.');
            selector = classes ? `${tag}.${classes}` : `${tag}:nth-of-type(${i + 1})`;
        }

        elements.push({
            selector: selector,
            label: label,
            type: isNav ? 'nav' : 'action',
            href: href,
            bbox: {
                x: Math.round(rect.x),
                y: Math.round(rect.y),
                w: Math.round(rect.width),
                h: Math.round(rect.height)
            }
        });
    });

    return elements;
}
"""

async def _capture_screenshot(page: Page) -> bytes:
    """Capture a full-page screenshot as PNG bytes."""
    return await page.screenshot(type="png", full_page=False)


def _compute_dom_hash(page_content: str) -> str:
    """Compute a hash of the page DOM for change detection."""
    return hashlib.sha256(page_content.encode()).hexdigest()[:16]


def _normalize_url(url: str, base_url: str) -> str:
    """Normalize a URL relative to the base dashboard URL."""
    parsed = urlparse(url)
    # skip external links, javascript:, mailto:, etc.
    if parsed.scheme and parsed.scheme not in ("http", "https", ""):
        return ""
    if not parsed.netloc and not parsed.path:
        return ""

    full_url = urljoin(base_url, url)
    # only crawl within the same domain
    base_domain = urlparse(base_url).netloc
    if urlparse(full_url).netloc != base_domain:
        return ""

    # strip fragments
    return full_url.split("#")[0]


async def _upload_screenshot(screenshot_bytes: bytes, company_id: str, page_id: str) -> str:
    """Upload screenshot to Cloudinary, return secure URL."""
    result = await upload_document(
        content=screenshot_bytes,
        filename=f"stroll_{company_id}_{page_id}.png",
        folder=f"stroll/{company_id}",
    )
    return result["secure_url"]


async def run_stroll(company_id: str, config: StrollConfig) -> StrollVersion:
    """
    Execute a full BFS crawl of the customer's dashboard.

    1. Health-check the dashboard URL
    2. BFS over navigation elements
    3. Screenshot + Claude vision analysis per page
    4. Build NavGraph with enriched labels and pre-computed instructions
    5. Upload screenshots to Cloudinary
    """
    global _browser
    if not _browser:
        await init_browser()

    context = await _browser.new_context(
        viewport={"width": 1280, "height": 800},
        ignore_https_errors=True,
    )

    graph = NavGraph()
    screenshot_urls: dict[str, str] = {}

    try:
        page = await context.new_page()

        # — health check —
        try:
            response = await page.goto(
                config.dashboard_url,
                timeout=settings.STROLL_PAGE_TIMEOUT_MS,
                wait_until="networkidle",
            )
            if not response or response.status >= 400:
                logger.error(f"Dashboard unhealthy: HTTP {response.status if response else 'no response'}")
                return StrollVersion(
                    id=f"stroll_{str(uuid4())[:8]}",
                    company_id=company_id,
                    timestamp=datetime.now(tz=timezone.utc),
                    graph=NavGraph(),
                    status="failed",
                )
        except Exception as e:
            logger.error(f"Dashboard health check failed: {e}")
            return StrollVersion(
                id=f"stroll_{str(uuid4())[:8]}",
                company_id=company_id,
                timestamp=datetime.now(tz=timezone.utc),
                graph=NavGraph(),
                status="failed",
            )

        # — handle authentication if credentials provided —
        if config.credentials:
            if config.credentials.pre_auth_url:
                await page.goto(
                    config.credentials.pre_auth_url,
                    timeout=settings.STROLL_PAGE_TIMEOUT_MS,
                    wait_until="networkidle",
                )
            # TODO: add form-based login flow if username/password provided

        # — BFS crawl —
        crawl_queue: deque[str] = deque([config.dashboard_url])
        visited: set[str] = set()
        max_pages = min(config.max_pages, settings.STROLL_MAX_PAGES)

        while crawl_queue and len(visited) < max_pages:
            current_url = crawl_queue.popleft()
            if current_url in visited:
                continue
            visited.add(current_url)

            try:
                await page.goto(
                    current_url,
                    timeout=settings.STROLL_PAGE_TIMEOUT_MS,
                    wait_until="networkidle",
                )
            except Exception as e:
                logger.warning(f"Failed to navigate to {current_url}: {e}")
                continue

            # generate stable page id from URL
            page_id = hashlib.sha256(current_url.encode()).hexdigest()[:12]

            # capture screenshot
            screenshot_bytes = await _capture_screenshot(page)

            # detect interactive elements from DOM
            raw_elements = await page.evaluate(DETECT_ELEMENTS_JS)

            # compute DOM hash for change detection
            dom_content = await page.content()
            dom_hash = _compute_dom_hash(dom_content)

            page_title = await page.title() or current_url

            # Claude vision analysis for rich understanding
            vision_result = await _analyze_page_with_vision(
                screenshot_bytes, raw_elements, page_title, current_url
            )

            # build enriched elements
            vision_elements_map = {
                e.get("selector", ""): e.get("human_description", "")
                for e in vision_result.get("elements", [])
            }

            elements: list[InteractiveElement] = []
            for raw in raw_elements:
                elements.append(InteractiveElement(
                    selector=raw["selector"],
                    label=raw.get("label", ""),
                    human_description=vision_elements_map.get(raw["selector"], ""),
                    type=raw.get("type", "nav"),
                    bbox=BoundingBox(**raw["bbox"]) if raw.get("bbox") else None,
                ))

            # create page node
            node = PageNode(
                id=page_id,
                url=current_url,
                title=page_title,
                page_summary=vision_result.get("page_summary", page_title),
                elements=elements,
                dom_hash=dom_hash,
            )
            graph.nodes[page_id] = node

            # upload screenshot
            screenshot_url = await _upload_screenshot(screenshot_bytes, company_id, page_id)
            screenshot_urls[page_id] = screenshot_url

            # discover navigation targets and build edges
            for elem in elements:
                if elem.type != "nav":
                    continue

                # find the raw element's href
                raw_match = next(
                    (r for r in raw_elements if r["selector"] == elem.selector),
                    None,
                )
                if not raw_match:
                    continue

                href = raw_match.get("href", "")
                if not href:
                    continue

                dest_url = _normalize_url(href, config.dashboard_url)
                if not dest_url or dest_url in visited:
                    # still add edge if we've already visited
                    dest_id = hashlib.sha256(dest_url.encode()).hexdigest()[:12] if dest_url else None
                    if dest_id and dest_id in graph.nodes:
                        edge = Edge(
                            from_page=page_id,
                            to_page=dest_id,
                            via=elem,
                            instruction=f"Click '{elem.label or elem.human_description}' on the {page_title} page",
                        )
                        graph.edges.append(edge)
                    continue

                dest_id = hashlib.sha256(dest_url.encode()).hexdigest()[:12]

                # add edge
                edge = Edge(
                    from_page=page_id,
                    to_page=dest_id,
                    via=elem,
                    instruction=f"Click '{elem.label or elem.human_description}' on the {page_title} page",
                )
                graph.edges.append(edge)

                # enqueue for crawling
                crawl_queue.append(dest_url)

    finally:
        await context.close()

    return StrollVersion(
        id=f"stroll_{str(uuid4())[:8]}",
        company_id=company_id,
        timestamp=datetime.now(tz=timezone.utc),
        graph=graph,
        screenshot_urls=screenshot_urls,
        status="success",
    )

def diff_stroll(new_graph: NavGraph, prev_version: Optional[StrollVersion]) -> DiffLog:
    """Compare new graph against previous version. Returns a DiffLog."""
    if prev_version is None:
        return DiffLog(
            added=list(new_graph.nodes.values()),
            removed=[],
            moved=[],
            unchanged_count=0,
        )

    prev_graph = prev_version.graph
    diff = DiffLog()

    # find added or changed pages
    for node_id, node in new_graph.nodes.items():
        if node_id not in prev_graph.nodes:
            diff.added.append(node)
        elif node.dom_hash != prev_graph.nodes[node_id].dom_hash:
            diff.added.append(node)  # treat changed page as updated
        else:
            diff.unchanged_count += 1

    # find removed pages
    for node_id in prev_graph.nodes:
        if node_id not in new_graph.nodes:
            diff.removed.append(node_id)

    # detect moved features (same label, different path from root)
    new_root = new_graph.get_root_id()
    prev_root = prev_graph.get_root_id()

    if new_root and prev_root:
        # collect all unique labels in both graphs
        new_labels: dict[str, str] = {}  # label → page_id
        for node in new_graph.nodes.values():
            for elem in node.elements:
                if elem.label:
                    new_labels[elem.label] = node.id

        prev_labels: dict[str, str] = {}
        for node in prev_graph.nodes.values():
            for elem in node.elements:
                if elem.label:
                    prev_labels[elem.label] = node.id

        for label in set(new_labels.keys()) & set(prev_labels.keys()):
            new_path = new_graph.shortest_path(new_root, new_labels[label])
            prev_path = prev_graph.shortest_path(prev_root, prev_labels[label])
            if new_path and prev_path and new_path != prev_path:
                diff.moved.append(MovedFeature(
                    label=label,
                    old_path=prev_path,
                    new_path=new_path,
                ))

    return diff

async def commit_stroll(
    company_id: str,
    version: StrollVersion,
    diff: DiffLog,
) -> Optional[StrollVersion]:
    """
    Persist a stroll version to MongoDB.
    Skips write if no changes detected.
    """
    if not diff.added and not diff.removed and not diff.moved:
        logger.info(f"No changes for company {company_id} — skipping write")
        return None

    version.diff = diff

    doc = version.model_dump()
    doc["_company_id"] = company_id  # for indexing

    await db.stroll_versions.insert_one(doc)
    logger.info(f"Committed stroll {version.id} for company {company_id}")

    return version

async def get_latest_version(company_id: str) -> Optional[StrollVersion]:
    """Get the most recent successful stroll version for a company."""
    doc = await db.stroll_versions.find_one(
        {"company_id": company_id, "status": "success"},
        sort=[("timestamp", -1)],
    )
    if doc:
        doc.pop("_id", None)
        return StrollVersion(**doc)
    return None


async def list_versions(company_id: str, limit: int = 10) -> list[dict]:
    """List stroll versions for a company (summary only)."""
    cursor = db.stroll_versions.find(
        {"company_id": company_id},
        {"graph": 0, "screenshot_urls": 0},  # exclude large fields
    ).sort("timestamp", -1).limit(limit)

    results = []
    async for doc in cursor:
        doc.pop("_id", None)
        results.append(doc)
    return results

async def get_stroll_config(company_id: str) -> Optional[StrollConfig]:
    """Get the stroll config for a company."""
    doc = await db.stroll_configs.find_one({"company_id": company_id})
    if doc:
        doc.pop("_id", None)
        return StrollConfig(**doc)
    return None


async def save_stroll_config(
    company_id: str, data: StrollConfigCreate
) -> StrollConfig:
    """Create or update stroll configuration for a company."""
    now = datetime.now(tz=timezone.utc)

    config_doc = {
        "company_id": company_id,
        **data.model_dump(),
        "updated_at": now,
    }

    await db.stroll_configs.update_one(
        {"company_id": company_id},
        {
            "$set": config_doc,
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )

    return StrollConfig(company_id=company_id, **data.model_dump(), updated_at=now)
