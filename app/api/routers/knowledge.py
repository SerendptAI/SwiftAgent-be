from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException, Query, UploadFile, File, Form
from typing import List, Optional
from app.core.auth import get_current_user
from app.models.knowledge_models import DocumentIngest, DocumentResponse, DocumentSummary, QueryRequest, QueryResponse, KnowledgeSourceResponse
from app.services import knowledge_service, cloudinary_service, text_extraction_service
from app.core.database import get_database
from datetime import datetime
from uuid import uuid4

router = APIRouter(tags=["Knowledge"])

@router.post("/", response_model=DocumentResponse)
async def ingest_document(
    document: DocumentIngest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
    db = Depends(get_database)
):
    """Ingest a new knowledge document."""
    user_id = current_user["user_id"]

    doc = document.model_dump()
    doc["user_id"] = user_id
    doc["id"] = str(uuid4())
    doc["created_at"] = datetime.utcnow()

    await db.documents.insert_one(doc)

    # background vector ingestion
    background_tasks.add_task(
        knowledge_service.ingest_document,
        user_id,
        doc["id"],
        doc["title"],
        doc["content"],
        doc.get("metadata", {}),
    )

    return doc

@router.get("/", response_model=List[DocumentSummary])
async def list_documents(
    current_user: dict = Depends(get_current_user),
    db = Depends(get_database)
):
    """List all knowledge documents for current user."""
    user_id = current_user["user_id"]
    cursor = db.documents.find({"user_id": user_id}, {"content": 0, "metadata": 0})
    documents = await cursor.to_list(length=100)
    return documents

@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: str,
    current_user: dict = Depends(get_current_user),
    db = Depends(get_database)
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
    current_user: dict = Depends(get_current_user)
):
    """Semantic search over knowledge base."""
    user_id = current_user["user_id"]
    results = await knowledge_service.search_knowledge(
        user_id,
        request.query,
        limit=request.limit,
        threshold=request.threshold,
    )
    return {"results": results, "confidence": confidence, "escalate": escalate}

@router.post("/upload", response_model=KnowledgeSourceResponse)
async def upload_knowledge_document(
    background_tasks: BackgroundTasks,
    company_id: str = Form(...),
    category: str = Form("general"),
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
    db = Depends(get_database)
):
    """Upload a document to Cloudinary and ingest its content as knowledge."""
    user_id = current_user["user_id"]
    
    filename = file.filename or "unknown_file"
    
    # read the file to bytes once for both operations
    content = await file.read()
    
    # try text extraction first before uploading
    try:
        extracted_text = text_extraction_service.extract_text(filename, content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    if not extracted_text.strip():
        raise HTTPException(status_code=400, detail="Could not extract any text from the document.")

    # pass the bytes to cloudinary since it accepts bytes
    await file.seek(0)
    
    # upload to cloudinary
    upload_result = await cloudinary_service.upload_document(
        file, folder=f"documents/{company_id}"
    )
    
    doc_id = str(uuid4())
    
    # store source record
    source_record = {
        "id": doc_id,
        "user_id": user_id,
        "company_id": company_id,
        "category": category,
        "filename": filename,
        "file_url": upload_result["secure_url"],
        "cloudinary_public_id": upload_result["public_id"],
        "uploaded_at": datetime.utcnow(),
    }
    await db.knowledge_sources.insert_one(source_record)
    
    # ingest the extracted text
    background_tasks.add_task(
        knowledge_service.ingest_document,
        user_id,
        doc_id,
        filename,
        extracted_text,
        {"company_id": company_id, "category": category, "source_id": doc_id, "file_url": upload_result["secure_url"]},
    )
    
    return source_record
