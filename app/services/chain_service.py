from app.models.diagnosis_models import DiagnosisRequest

async def diagnose_transaction(request: DiagnosisRequest) -> dict:
    # stub for phase 3 — on-chain transaction diagnosis
    # will integrate blockchain explorer apis for evm chains
    return {
        "status": "pending",
        "diagnosis": "Transaction diagnosis is not yet implemented.",
        "details": {
            "tx_hash": request.tx_hash,
            "chain_id": request.chain_id,
            "wallet_address": request.wallet_address,
            "note": "This feature will be available in Phase 3.",
        },
    }
