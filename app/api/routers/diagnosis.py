from fastapi import APIRouter, Depends
from app.core.auth import get_current_user
from app.models.diagnosis_models import DiagnosisRequest, DiagnosisResponse
from app.services import chain_service

router = APIRouter(tags=["Diagnosis"])

@router.post("/", response_model=DiagnosisResponse)
async def diagnose_transaction(
    request: DiagnosisRequest,
    current_user: dict = Depends(get_current_user)
):
    """Diagnose an on-chain transaction."""
    result = await chain_service.diagnose_transaction(request)
    return result
