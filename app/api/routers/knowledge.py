from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException, Query
from typing import List, Optional
from app.core.auth import get_current_user
from app.models.knowledge_models import DocumentIngest, DocumentResponse, DocumentSummary, QueryRequest, QueryResponse
from app.services import knowledge_service
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
    return results
