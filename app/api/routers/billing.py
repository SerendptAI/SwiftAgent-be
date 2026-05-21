import json
import base64
import hmac
import hashlib
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks
from app.core.auth import get_current_user
from app.core.database import db
from app.models.billing_models import CheckoutSessionRequest, CheckoutSessionResponse, WebhookResponse
from app.services.billing_service import billing_service
from app.core.billing_limits import TIER_LIMITS
from app.core.config import settings
from app.core.plan_enforcement import get_usage_summary
from app.services.company_service import get_company

router = APIRouter(prefix="/billing", tags=["Billing"])
logger = logging.getLogger(__name__)


@router.get("/plans")
async def get_billing_plans():
    """Retrieve available billing plans and their limits."""
    return TIER_LIMITS


@router.get("/{company_id}/status")
async def get_billing_status(
    company_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Retrieve the company's current subscription status and usage."""
    user_id = current_user["user_id"]
    company = await get_company(company_id, user_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    summary = await get_usage_summary(company)
    return summary


@router.post("/checkout", response_model=CheckoutSessionResponse)
async def create_checkout_session(
    request: CheckoutSessionRequest,
    current_user: dict = Depends(get_current_user)
):
    """Generate a checkout session (PalmPay or Polar) for a company paying for a tier."""
    company = await db.companies.find_one({"id": request.company_id, "user_id": current_user["user_id"]})
    
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    email = company.get("contact_email") or current_user.get("email")
    country = company.get("country", "")

    try:
        checkout_url = await billing_service.create_checkout_session(
            company_id=request.company_id,
            tier=request.tier,
            country=country,
            email=email
        )
        return CheckoutSessionResponse(checkout_url=checkout_url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Checkout session creation failed: {e}")
        raise HTTPException(status_code=500, detail="Unable to start checkout. Please try again or contact support.")


@router.post("/webhooks/palmpay", response_model=WebhookResponse)
async def palmpay_webhook(request: Request, background_tasks: BackgroundTasks):
    """Handle PalmPay webhooks — verified via RSA-SHA256."""
    payload_body = await request.body()
    signature_b64 = request.headers.get("Signature", "")

    palmpay_pub_key_pem = settings.PALMPAY_PALMPAY_PUBLIC_KEY
    if palmpay_pub_key_pem and signature_b64:
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding

            palmpay_pub_key = serialization.load_pem_public_key(
                palmpay_pub_key_pem.encode()
            )
            palmpay_pub_key.verify(
                base64.b64decode(signature_b64),
                payload_body,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid PalmPay signature")
    elif not palmpay_pub_key_pem:
        raise HTTPException(status_code=500, detail="Payment verification service is temporarily unavailable.")

    payload = json.loads(payload_body)
    background_tasks.add_task(billing_service.process_palmpay_webhook, payload)

    return WebhookResponse(received=True)


@router.post("/webhooks/polar", response_model=WebhookResponse)
async def polar_webhook(request: Request, background_tasks: BackgroundTasks):
    """Handle Polar webhooks."""
    signature = request.headers.get("webhook-signature")
    webhook_secret = settings.POLAR_WEBHOOK_SECRET

    if not webhook_secret:
        raise HTTPException(status_code=500, detail="Payment verification service is temporarily unavailable.")

    if not signature:
        raise HTTPException(status_code=400, detail="Missing signature")

    payload_body = await request.body()
    hash_digest = hmac.new(
        webhook_secret.encode("utf-8"),
        payload_body,
        hashlib.sha256
    ).hexdigest()

    if hash_digest != signature:
        raise HTTPException(status_code=400, detail="Invalid signature")

    payload = await request.json()
    background_tasks.add_task(billing_service.process_polar_webhook, payload)

    return WebhookResponse(received=True)
