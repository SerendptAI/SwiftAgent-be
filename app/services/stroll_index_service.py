"""
Stroll Index Service — semantic search over dashboard navigation.

Builds a vector index from stroll data (page summaries + element labels)
and answers "where is X?" queries with annotated screenshot guides.
"""

import logging
from io import BytesIO
from typing import Optional
from uuid import uuid4

import httpx
from PIL import Image, ImageDraw
from google import genai
from qdrant_client.http import models

from app.core.config import settings
from app.core.database import qdrant_client
from app.models.stroll_models import (
    BoundingBox,
    FindFeatureResult,
    NavigationStep,
    NavGraph,
    StrollVersion,
)
from app.services.cloudinary_service import upload_document
from app.services.stroll_service import get_latest_version

logger = logging.getLogger(__name__)

COLLECTION_NAME = "stroll_nav_index"


def _get_gemini_client() -> genai.Client:
    return genai.Client(api_key=settings.GEMINI_API_KEY)

async def _ensure_collection():
    """Create the stroll nav index collection if it doesn't exist."""
    if not await qdrant_client.collection_exists(COLLECTION_NAME):
        await qdrant_client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=models.VectorParams(
                size=3072,  # Gemini text-embedding-001 dimension
                distance=models.Distance.COSINE,
            ),
        )

    # ensure payload indexes
    for field in ("company_id", "node_id"):
        await qdrant_client.create_payload_index(
            collection_name=COLLECTION_NAME,
            field_name=field,
            field_schema="keyword",
        )

async def build_index(company_id: str, version: StrollVersion):
    """
    Build/rebuild the semantic search index from a stroll version.

    For each page node, embeds:
    - Page title + summary
    - All element labels + human descriptions
    Then upserts into Qdrant.
    """
    await _ensure_collection()

    gemini_client = _get_gemini_client()

    # first, delete old entries for this company
    await qdrant_client.delete(
        collection_name=COLLECTION_NAME,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="company_id",
                        match=models.MatchValue(value=company_id),
                    )
                ]
            )
        ),
    )

    points = []
    root_id = version.graph.get_root_id()

    for node_id, node in version.graph.nodes.items():
        # build searchable text from all labels on this page
        labels = [node.title, node.page_summary]
        for elem in node.elements:
            if elem.label:
                labels.append(elem.label)
            if elem.human_description:
                labels.append(elem.human_description)

        text_to_embed = " | ".join(labels)

        try:
            response = await gemini_client.aio.models.embed_content(
                model="gemini-embedding-001",
                contents=text_to_embed,
                config={"task_type": "RETRIEVAL_DOCUMENT"},
            )
            vector = response.embeddings[0].values
        except Exception as e:
            logger.warning(f"Embedding failed for node {node_id}: {e}")
            continue

        # compute path from root for context
        path_from_root = []
        if root_id:
            path_ids = version.graph.shortest_path(root_id, node_id)
            if path_ids:
                path_from_root = [
                    version.graph.nodes[pid].title
                    for pid in path_ids
                    if pid in version.graph.nodes
                ]

        points.append(
            models.PointStruct(
                id=str(uuid4()),
                vector=vector,
                payload={
                    "company_id": company_id,
                    "node_id": node_id,
                    "page_title": node.title,
                    "page_summary": node.page_summary,
                    "labels": labels,
                    "path_from_root": path_from_root,
                    "url": node.url,
                },
            )
        )

    if points:
        # batch upsert
        batch_size = 50
        for i in range(0, len(points), batch_size):
            await qdrant_client.upsert(
                collection_name=COLLECTION_NAME,
                points=points[i : i + batch_size],
            )

    logger.info(f"Built stroll index for company {company_id}: {len(points)} pages indexed")

def _annotate_screenshot(
    image_bytes: bytes,
    highlight: Optional[BoundingBox],
    is_destination: bool = False,
) -> bytes:
    """
    Draw a highlight rectangle (and optionally an arrow) on a screenshot.
    Returns annotated PNG bytes.
    """
    img = Image.open(BytesIO(image_bytes))
    draw = ImageDraw.Draw(img)

    if highlight:
        x, y, w, h = highlight.x, highlight.y, highlight.w, highlight.h

        if is_destination:
            # green border for destination
            for offset in range(3):
                draw.rectangle(
                    [x - offset, y - offset, x + w + offset, y + h + offset],
                    outline=(34, 197, 94),  # green
                )
        else:
            # red/orange border with slight glow for "click here"
            for offset in range(3):
                draw.rectangle(
                    [x - offset, y - offset, x + w + offset, y + h + offset],
                    outline=(239, 68, 68),  # red
                )
            # simple arrow indicator (triangle pointing to the element)
            arrow_x = x + w // 2
            arrow_y = y - 20
            draw.polygon(
                [(arrow_x - 8, arrow_y), (arrow_x + 8, arrow_y), (arrow_x, y - 4)],
                fill=(239, 68, 68),
            )

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


async def _download_screenshot(url: str) -> bytes:
    """Download a screenshot from Cloudinary."""
    async with httpx.AsyncClient() as client:
        response = await client.get(url, timeout=10)
        response.raise_for_status()
        return response.content

async def find_feature(
    company_id: str, user_query: str
) -> Optional[FindFeatureResult]:
    """
    Search for a feature in the stroll data and return an annotated
    step-by-step visual guide.
    """
    version = await get_latest_version(company_id)
    if not version:
        return None

    # ensure collection exists
    if not await qdrant_client.collection_exists(COLLECTION_NAME):
        return None

    # embed query
    gemini_client = _get_gemini_client()
    try:
        response = await gemini_client.aio.models.embed_content(
            model="gemini-embedding-001",
            contents=user_query,
            config={"task_type": "RETRIEVAL_QUERY"},
        )
        query_vector = response.embeddings[0].values
    except Exception as e:
        logger.error(f"Query embedding failed: {e}")
        return None

    # search
    search_result = await qdrant_client.query_points(
        collection_name=COLLECTION_NAME,
        query=query_vector,
        query_filter=models.Filter(
            must=[
                models.FieldCondition(
                    key="company_id",
                    match=models.MatchValue(value=company_id),
                )
            ]
        ),
        limit=1,
    )

    hits = search_result.points
    if not hits or hits[0].score < 0.5:
        return None

    target_node_id = hits[0].payload["node_id"]

    # compute shortest path from root to target
    root_id = version.graph.get_root_id()
    if not root_id:
        return None

    path_ids = version.graph.shortest_path(root_id, target_node_id)
    if not path_ids:
        return None

    # build annotated steps
    steps: list[NavigationStep] = []

    for i, node_id in enumerate(path_ids):
        node = version.graph.nodes.get(node_id)
        if not node:
            continue

        screenshot_url = version.screenshot_urls.get(node_id, "")
        is_last = i == len(path_ids) - 1

        # find the element to highlight (the one leading to next step)
        highlight = None
        instruction = ""

        if not is_last:
            next_id = path_ids[i + 1]
            edge = version.graph.edge_between(node_id, next_id)
            if edge:
                instruction = edge.instruction
                highlight = edge.via.bbox
            else:
                instruction = f"Navigate to the next page from {node.title}"
        else:
            instruction = f"You've arrived at {node.title}. {node.page_summary}"

        # annotate the screenshot if we have both the URL and a highlight
        annotated_url = screenshot_url
        if screenshot_url and highlight:
            try:
                raw_bytes = await _download_screenshot(screenshot_url)
                annotated_bytes = _annotate_screenshot(raw_bytes, highlight, is_destination=is_last)
                result = await upload_document(
                    content=annotated_bytes,
                    filename=f"annotated_{node_id}_step{i+1}.png",
                    folder=f"stroll/{company_id}/annotated",
                )
                annotated_url = result["secure_url"]
            except Exception as e:
                logger.warning(f"Failed to annotate screenshot for {node_id}: {e}")

        steps.append(NavigationStep(
            step=i + 1,
            page_title=node.title,
            instruction=instruction,
            screenshot_url=annotated_url,
            highlight=highlight,
        ))

    path_summary = [
        version.graph.nodes[nid].title
        for nid in path_ids
        if nid in version.graph.nodes
    ]

    return FindFeatureResult(path_summary=path_summary, steps=steps)
