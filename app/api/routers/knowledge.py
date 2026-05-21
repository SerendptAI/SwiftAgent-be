import logging
from datetime import datetime, timezone
from uuid import uuid4
from fastapi import (
    APIRouter,
    Depends,
    BackgroundTasks,
    HTTPException,
    Query,
    UploadFile,
    File,
    Form,
)
from typing import Optional
from app.core.auth import get_current_user
from app.models.knowledge_models import (
    DocumentIngest,
    DocumentResponse,
    DocumentSummary,
    QueryRequest,
    QueryResponse,
    KnowledgeSourceResponse,
)
from app.services import knowledge_service, cloudinary_service, text_extraction_service, company_service
from app.core.database import get_database
from app.core.config import settings
from app.core.plan_enforcement import enforce_document_limit

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Knowledge"])


@router.post("/", response_model=DocumentResponse, status_code=201)
async def ingest_document(
    document: DocumentIngest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_database),
):
    """Ingest a new knowledge document."""
    user_id = current_user["user_id"]

    doc = document.model_dump()

    # Plan enforcement: check document limit if company_id is provided
    company_id = doc.get("metadata", {}).get("company_id")
    if company_id:
        company = await company_service.get_company(company_id, user_id)
        if company:
            await enforce_document_limit(company)

    doc["user_id"] = user_id
    doc["id"] = str(uuid4())
    doc["created_at"] = datetime.now(tz=timezone.utc)

    await db.documents.insert_one(doc)

    def _on_ingest_error(task):
        import logging

        if task.exception():
            logging.getLogger(__name__).error(
                f"Background knowledge ingestion failed for doc {doc['id']}: {task.exception()}"
            )

    task = background_tasks.add_task(
        knowledge_service.ingest_document,
        user_id,
        doc["id"],
        doc["title"],
        doc["content"],
        doc.get("metadata", {}),
    )
    return doc


@router.get("/")
async def list_documents(
    current_user: dict = Depends(get_current_user),
    db=Depends(get_database),
    limit: int = Query(
        default=settings.DEFAULT_PAGE_LIMIT, ge=1, le=settings.MAX_PAGE_LIMIT
    ),
    skip: int = Query(default=0, ge=0),
):
    """List all knowledge documents for current user."""
    user_id = current_user["user_id"]
    cursor = (
        db.documents.find({"user_id": user_id}, {"content": 0, "metadata": 0})
        .skip(skip)
        .limit(limit)
    )
    documents = await cursor.to_list(length=limit)

    total = await db.documents.count_documents({"user_id": user_id})
    return {
        "items": documents,
        "total": total,
        "limit": limit,
        "skip": skip,
        "has_next": (skip + len(documents)) < total,
    }


@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_database),
):
    """Get a specific knowledge document by ID."""
    user_id = current_user["user_id"]
    document = await db.documents.find_one({"id": document_id, "user_id": user_id})

    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    return document


@router.post("/query", response_model=QueryResponse)
async def query_knowledge(
    request: QueryRequest,
    current_user: dict = Depends(get_current_user),
):
    """Semantic search over knowledge base."""
    user_id = current_user["user_id"]
    results = await knowledge_service.search_knowledge(
        user_id,
        request.query,
        limit=request.limit,
        threshold=request.threshold,
        company_id=request.company_id,
    )
    return results


@router.post("/upload", response_model=KnowledgeSourceResponse, status_code=201)
async def upload_knowledge_document(
    background_tasks: BackgroundTasks,
    company_id: str = Form(...),
    category: str = Form("general"),
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
    db=Depends(get_database),
):
    """Upload a document to Cloudinary and ingest its content as knowledge."""
    user_id = current_user["user_id"]

    # Plan enforcement: check document limit
    company = await company_service.get_company(company_id, user_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    await enforce_document_limit(company)

    filename = file.filename or "unknown_file"

    content = await file.read()
    if len(content) > settings.MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Maximum size is {settings.MAX_UPLOAD_SIZE_BYTES / (1024 * 1024):.0f}MB",
        )

    try:
        extracted_text = text_extraction_service.extract_text(filename, content)
    except ValueError as e:
        logger.warning(f"Unsupported file upload attempt for company {company_id}: {e}")
        raise HTTPException(status_code=400, detail="Unsupported file type. We support PDF, DOCX, TXT, and CSV files.")

    if not extracted_text.strip():
        raise HTTPException(
            status_code=400, detail="Could not extract any text from the document."
        )

    upload_result = await cloudinary_service.upload_document(
        content, filename, folder=f"documents/{company_id}"
    )

    doc_id = str(uuid4())

    source_record = {
        "id": doc_id,
        "user_id": user_id,
        "company_id": company_id,
        "category": category,
        "filename": filename,
        "file_url": upload_result["secure_url"],
        "cloudinary_public_id": upload_result["public_id"],
        "uploaded_at": datetime.now(tz=timezone.utc),
    }
    await db.knowledge_sources.insert_one(source_record)

    background_tasks.add_task(
        knowledge_service.ingest_document,
        user_id,
        doc_id,
        filename,
        extracted_text,
        {
            "company_id": company_id,
            "category": category,
            "source_id": doc_id,
            "file_url": upload_result["secure_url"],
        },
    )

    return source_record
