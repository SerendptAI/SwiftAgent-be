import logging
from fastapi import APIRouter, Depends
from app.core.auth import get_current_user
from app.core.rbac import require_permission
from app.models.diagnosis_models import DiagnosisRequest, DiagnosisResponse
from app.services import chain_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Diagnosis"])


@router.post("/", response_model=DiagnosisResponse)
async def diagnose_transaction(
    request: DiagnosisRequest,
    current_user: dict = Depends(require_permission("tickets:read")),
):
    """Diagnose an on-chain transaction."""
    try:
        result = await chain_service.diagnose_transaction(request)
        return result
    except Exception as e:
        logger.exception("Diagnosis failed")
        return {
            "overall_severity": "info",
            "issues_found": 1,
            "issues": [
                {
                    "issue": "Diagnosis service unavailable",
                    "severity": "warning",
                    "explanation": "Unable to analyze transaction at this time.",
                    "action": "Please try again later or check the transaction on a block explorer.",
                }
            ],
            "tx_summary": {
                "chain": "unknown",
                "status": "unknown",
                "tx_hash": getattr(request, "tx_hash", ""),
                "from": "",
                "to": "",
            },
        }
