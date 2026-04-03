"""
Cloudinary file upload service.
Handles image uploads (logos) and raw document uploads (PDFs, DOCX, TXT, etc.).
"""

import asyncio
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
