import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional

from app.core.sdk_auth import verify_api_key
from app.services import chain_service
from app.services import dashboard_service
from app.services import knowledge_service
from app.services import stroll_service
from app.models.diagnosis_models import DiagnosisRequest, DiagnosisResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(tags=["WebMCP"])

# Request model for Knowledge Base query
class KnowledgeQueryRequest(BaseModel):
    query: str
    limit: int = 5
    threshold: float = 0.7

@router.post("/diagnose", response_model=DiagnosisResponse)
async def webmcp_diagnose(
    request: DiagnosisRequest,
    company: dict = Depends(verify_api_key),
):
    """Diagnose a blockchain transaction."""
    try:
        return await chain_service.diagnose_transaction(request)
    except Exception as e:
        logger.exception("WebMCP Diagnosis failed")
        raise HTTPException(status_code=500, detail="Diagnosis service unavailable")

@router.get("/stats")
async def webmcp_stats(company: dict = Depends(verify_api_key)):
    """Fetch live dashboard stats."""
    try:
        company_id = company["id"]
        return await dashboard_service.get_stats(company_id)
    except Exception as e:
        logger.exception("WebMCP Stats failed")
        raise HTTPException(status_code=500, detail="Stats service unavailable")

@router.get("/visitors")
async def webmcp_visitors(limit: int = 10, company: dict = Depends(verify_api_key)):
    """Fetch recent dashboard visitors."""
    try:
        company_id = company["id"]
        return await dashboard_service.get_visitors(company_id, limit=limit)
    except Exception as e:
        logger.exception("WebMCP Visitors failed")
        raise HTTPException(status_code=500, detail="Visitors service unavailable")

@router.post("/query")
async def webmcp_query(
    request: KnowledgeQueryRequest,
    company: dict = Depends(verify_api_key),
):
    """Semantic search over knowledge base."""
    try:
        # Pass the creator's user_id since documents are tied to the creator
        user_id = company.get("created_by")
        company_id = company["id"]
        
        results = await knowledge_service.search_knowledge(
            user_id=user_id,
            query=request.query,
            limit=request.limit,
            threshold=request.threshold,
            company_id=company_id,
        )
        return results
    except Exception as e:
        logger.exception("WebMCP Knowledge Query failed")
        raise HTTPException(status_code=500, detail="Knowledge service unavailable")

@router.get("/navigation")
async def webmcp_navigation(company: dict = Depends(verify_api_key)):
    """Fetch the latest Stroll Navigation Graph."""
    try:
        company_id = company["id"]
        version = await stroll_service.get_latest_version(company_id)
        if not version:
            return {"nodes": {}, "edges": []}
            
        # Return the raw graph as a dict
        return version.graph.model_dump()
    except Exception as e:
        logger.exception("WebMCP Navigation failed")
        raise HTTPException(status_code=500, detail="Navigation service unavailable")
