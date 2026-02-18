from typing import Any, Dict, Optional
from pydantic import BaseModel

class DiagnosisRequest(BaseModel):
    tx_hash: str
    chain_id: int
    wallet_address: Optional[str] = None

class DiagnosisResponse(BaseModel):
    status: str
    diagnosis: str
    details: Dict[str, Any] = {}
