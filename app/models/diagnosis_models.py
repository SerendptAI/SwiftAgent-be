from typing import Any, Dict, Optional
from pydantic import BaseModel

class DiagnosisRequest(BaseModel):
    tx_hash: str
    chain_id: int
    wallet_address: Optional[str] = None

class DiagnosisResponse(BaseModel):
    overall_severity: str
    issues_found: int
    issues: list
    tx_summary: Dict[str, Any]
