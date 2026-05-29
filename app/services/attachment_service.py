"""
Shared attachment processing for all agent services.

Handles downloading attachments from Cloudinary URLs and converting them into
the formats required by each AI provider (Anthropic, Gemini, OpenRouter).

The extracted/encoded content is returned alongside raw metadata so it can be
persisted in conversation history — surviving even after the Cloudinary upload
expires (2-hour TTL).
"""

import base64
import logging

import httpx
from google.genai import types

from app.services.text_extraction_service import extract_text

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "bmp"}


def _get_ext(filename: str) -> str:
    """Extract lowercase file extension from a filename."""
    return filename.rsplit(".", 1)[-1].lower() if filename and "." in filename else ""


def _is_image(att: dict) -> bool:
    """Check if an attachment is an image based on type or mime_type."""
    if att.get("type") == "image":
        return True
    mime = att.get("mime_type", "")
    return mime.startswith("image/")


async def download_file(url: str, timeout: float = 30.0) -> bytes:
    """Download file content from a URL."""
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.content


async def extract_attachment_content(attachments: list[dict]) -> list[dict]:
    """
    Download each attachment and extract its content (base64 for images,
    text for documents). Returns enriched attachment dicts with an
    'extracted_content' field suitable for conversation persistence.

    This is the canonical function — call it once at the start of
    chat_stream, then pass the enriched list to the provider-specific
    formatter AND to conversation save.
    """
    enriched = []
    for att in attachments:
        try:
            url = att.get("url", "")
            filename = att.get("filename", "file")
            mime = att.get("mime_type", "")

            if not url or not url.startswith("https://res.cloudinary.com/"):
                logger.warning(f"Skipping attachment with invalid/non-Cloudinary URL: {url}")
                continue

            data = await download_file(url, timeout=60.0)

            if _is_image(att):
                b64 = base64.standard_b64encode(data).decode("utf-8")
                enriched.append({
                    **att,
                    "extracted_content": b64,
                    "content_encoding": "base64",
                })
            else:
                text = extract_text(filename, data)
                enriched.append({
                    **att,
                    "extracted_content": text,
                    "content_encoding": "text",
                })
        except Exception as e:
            logger.warning(f"Failed to process attachment '{att.get('filename')}': {e}")
            # Still include the attachment metadata even if extraction fails
            enriched.append({**att, "extracted_content": None, "content_encoding": None})

    return enriched


# ---------------------------------------------------------------------------
# Provider-specific formatters
# ---------------------------------------------------------------------------
# These take already-enriched attachments (with 'extracted_content') and
# convert them to the content-block format each provider API expects.
# ---------------------------------------------------------------------------


def format_for_anthropic(enriched_attachments: list[dict]) -> list[dict]:
    """Convert enriched attachments to Anthropic content blocks."""
    blocks = []
    for att in enriched_attachments:
        content = att.get("extracted_content")
        if content is None:
            continue

        if att.get("content_encoding") == "base64":
            blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": att.get("mime_type", "image/jpeg"),
                    "data": content,
                },
            })
        else:
            blocks.append({
                "type": "text",
                "text": f"[Attached Document: {att.get('filename', 'file')}]\n{content}",
            })
    return blocks


def format_for_gemini(enriched_attachments: list[dict]) -> list:
    """Convert enriched attachments to Gemini Part objects."""
    parts = []
    for att in enriched_attachments:
        content = att.get("extracted_content")
        if content is None:
            continue

        if att.get("content_encoding") == "base64":
            raw_bytes = base64.standard_b64decode(content)
            mime = att.get("mime_type", "image/jpeg")
            parts.append(types.Part.from_bytes(data=raw_bytes, mime_type=mime))
        else:
            ext = _get_ext(att.get("filename", ""))
            if ext == "pdf":
                # Gemini can handle PDF natively via bytes
                try:
                    url = att.get("url", "")
                    # We already have extracted text, use that
                    parts.append(types.Part.from_text(
                        text=f"[Attached Document: {att.get('filename', 'file')}]\n{content}"
                    ))
                except Exception:
                    parts.append(types.Part.from_text(
                        text=f"[Attached Document: {att.get('filename', 'file')}]\n{content}"
                    ))
            else:
                parts.append(types.Part.from_text(
                    text=f"[Attached Document: {att.get('filename', 'file')}]\n{content}"
                ))
    return parts


def format_for_openrouter(enriched_attachments: list[dict]) -> list[dict]:
    """Convert enriched attachments to OpenAI-compatible content parts."""
    parts = []
    for att in enriched_attachments:
        content = att.get("extracted_content")
        if content is None:
            continue

        if att.get("content_encoding") == "base64":
            mime = att.get("mime_type", "image/jpeg")
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{content}"},
            })
        else:
            parts.append({
                "type": "text",
                "text": f"[Attached Document: {att.get('filename', 'file')}]\n{content}",
            })
    return parts


def build_persistable_attachments(enriched_attachments: list[dict]) -> list[dict]:
    """
    Build the attachment list to persist in conversation history.

    Stores the extracted content (text or base64) so conversation context
    survives after Cloudinary files expire (2-hour TTL). The original URL
    is also kept for reference but should not be relied upon after expiry.
    """
    persistable = []
    for att in enriched_attachments:
        persistable.append({
            "url": att.get("url", ""),
            "type": att.get("type", ""),
            "mime_type": att.get("mime_type", ""),
            "filename": att.get("filename", ""),
            "extracted_content": att.get("extracted_content"),
            "content_encoding": att.get("content_encoding"),
        })
    return persistable
