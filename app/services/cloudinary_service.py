"""
Cloudinary file upload service.
Handles image uploads (logos) and raw document uploads (PDFs, DOCX, TXT, etc.).
"""

import asyncio
import logging
import cloudinary
import cloudinary.uploader
from fastapi import UploadFile
from app.core.config import settings

cloudinary.config(
    cloud_name=settings.CLOUDINARY_CLOUD_NAME,
    api_key=settings.CLOUDINARY_API_KEY,
    api_secret=settings.CLOUDINARY_API_SECRET,
    secure=True,
)


async def upload_image(file: UploadFile, folder: str = "logos") -> str:
    """Upload an image file to Cloudinary and return its secure URL."""
    contents = await file.read()
    result = await asyncio.to_thread(
        cloudinary.uploader.upload,
        contents,
        folder=folder,
        resource_type="image",
        transformation=[
            {"quality": "auto", "fetch_format": "auto"},
        ],
    )
    return result["secure_url"]


async def upload_document(
    content: bytes, filename: str, folder: str = "documents"
) -> dict:
    """Upload a raw document to Cloudinary. Returns dict with 'secure_url' and 'public_id'."""
    result = await asyncio.to_thread(
        cloudinary.uploader.upload,
        content,
        folder=folder,
        resource_type="raw",
        original_filename=filename,
        use_filename=True,
        unique_filename=True,
    )
    return {
        "secure_url": result["secure_url"],
        "public_id": result["public_id"],
    }


async def delete_file(public_id: str, resource_type: str = "image") -> bool:
    """Delete a file from Cloudinary by its public ID."""
    result = await asyncio.to_thread(
        cloudinary.uploader.destroy, public_id, resource_type=resource_type
    )
    return result.get("result") == "ok"


# ---------------------------------------------------------------------------
# Chat attachment uploads with auto-cleanup TTL
# ---------------------------------------------------------------------------

_CHAT_TTL_SECONDS = 2 * 60 * 60  # 2 hours


async def _schedule_delete(public_id: str, resource_type: str, delay: int = _CHAT_TTL_SECONDS):
    """Background coroutine: waits *delay* seconds then deletes the asset."""
    logger = logging.getLogger(__name__)
    try:
        await asyncio.sleep(delay)
        await delete_file(public_id, resource_type)
        logger.info(f"TTL cleanup: deleted {resource_type}/{public_id}")
    except Exception as e:
        logger.warning(f"TTL cleanup failed for {public_id}: {e}")


async def upload_chat_image(file: UploadFile, folder: str = "chat_images") -> dict:
    """
    Upload an image for chat and schedule deletion after TTL.
    Returns dict with 'secure_url' and 'public_id'.
    """
    contents = await file.read()
    result = await asyncio.to_thread(
        cloudinary.uploader.upload,
        contents,
        folder=folder,
        resource_type="image",
        transformation=[
            {"quality": "auto", "fetch_format": "auto"},
        ],
    )
    # Schedule background cleanup
    asyncio.create_task(_schedule_delete(result["public_id"], "image"))
    return {
        "secure_url": result["secure_url"],
        "public_id": result["public_id"],
    }


async def upload_chat_document(
    content: bytes, filename: str, folder: str = "chat_documents"
) -> dict:
    """
    Upload a document for chat and schedule deletion after TTL.
    Returns dict with 'secure_url' and 'public_id'.
    """
    result = await asyncio.to_thread(
        cloudinary.uploader.upload,
        content,
        folder=folder,
        resource_type="raw",
        original_filename=filename,
        use_filename=True,
        unique_filename=True,
    )
    # Schedule background cleanup
    asyncio.create_task(_schedule_delete(result["public_id"], "raw"))
    return {
        "secure_url": result["secure_url"],
        "public_id": result["public_id"],
    }
