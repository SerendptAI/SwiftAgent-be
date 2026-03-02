"""
Cloudinary file upload service.
Handles image uploads (logos) and raw document uploads (PDFs, DOCX, TXT, etc.).
"""

import cloudinary
import cloudinary.uploader
from fastapi import UploadFile
from app.core.config import settings

# configure cloudinary sdk on module load
cloudinary.config(
    cloud_name=settings.CLOUDINARY_CLOUD_NAME,
    api_key=settings.CLOUDINARY_API_KEY,
    api_secret=settings.CLOUDINARY_API_SECRET,
    secure=True,
)


async def upload_image(file: UploadFile, folder: str = "logos") -> str:
    """
    Upload an image file to Cloudinary and return its secure URL.
    Applies automatic format and quality optimisation.
    """
    contents = await file.read()
    result = cloudinary.uploader.upload(
        contents,
        folder=folder,
        resource_type="image",
        transformation=[
            {"quality": "auto", "fetch_format": "auto"},
        ],
    )
    return result["secure_url"]


async def upload_document(file: UploadFile, folder: str = "documents") -> dict:
    """
    Upload a raw document (PDF, DOCX, TXT, CSV, etc.) to Cloudinary.
    Returns a dict with 'secure_url' and 'public_id'.
    """
    contents = await file.read()
    result = cloudinary.uploader.upload(
        contents,
        folder=folder,
        resource_type="raw",
        original_filename=file.filename,
        use_filename=True,
        unique_filename=True,
    )
    return {
        "secure_url": result["secure_url"],
        "public_id": result["public_id"],
    }


async def delete_file(public_id: str, resource_type: str = "image") -> bool:
    """Delete a file from Cloudinary by its public ID."""
    result = cloudinary.uploader.destroy(public_id, resource_type=resource_type)
    return result.get("result") == "ok"
