from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from datetime import datetime

class DocumentIngest(BaseModel):
    title: str
    content: str
    company_id: Optional[str] = None
    category: Optional[str] = None
    metadata: Dict[str, Any] = {}

class DocumentResponse(BaseModel):
    id: str
    user_id: str
    title: str
    content: str
    company_id: Optional[str] = None
    category: Optional[str] = None
    metadata: Dict[str, Any]
    created_at: datetime

class DocumentSummary(BaseModel):
    id: str
    user_id: str
    title: str
    company_id: Optional[str] = None
    category: Optional[str] = None
    created_at: datetime

class SearchResult(BaseModel):
    content: str
    title: str
    score: float
    metadata: Dict[str, Any]

class QueryRequest(BaseModel):
    query: str
    company_id: Optional[str] = None
    limit: int = Field(5, ge=1, description="Max results to return")
    threshold: float = Field(0.7, ge=0.0, le=1.0, description="Minimum similarity score")

class QueryResponse(BaseModel):
    results: List[SearchResult]
    confidence: float = Field(description="Overall confidence score")
    escalate: bool = Field(False, description="True if confidence is below threshold")

class KnowledgeSourceUpload(BaseModel):
    category: str
    company_id: str

class KnowledgeSourceResponse(BaseModel):
    id: str
    company_id: str
    category: str
    filename: str
    uploaded_at: datetime
