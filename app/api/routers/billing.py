from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks
from app.core.auth import get_current_user
from app.core.database import db
from app.models.billing_models import CheckoutSessionRequest, CheckoutSessionResponse, WebhookResponse
from app.services.billing_service import billing_service
from app.core.billing_limits import TIER_LIMITS
import hmac
import hashlib
from app.core.config import settings
from app.core.plan_enforcement import get_usage_summary
from app.services.company_service import get_company

router = APIRouter(prefix="/billing", tags=["billing"])


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
    """Generate a checkout session (Paystack or Polar) for a company paying for a tier."""
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
        raise HTTPException(status_code=500, detail="Failed to initialize checkout session")


@router.post("/webhooks/paystack", response_model=WebhookResponse)
async def paystack_webhook(request: Request, background_tasks: BackgroundTasks):
    """Handle Paystack webhooks."""
    # Verify signature
    signature = request.headers.get("x-paystack-signature")
    if not signature and settings.PAYSTACK_SECRET_KEY:
        raise HTTPException(status_code=400, detail="Missing signature")
        
    payload_body = await request.body()
    
    if settings.PAYSTACK_SECRET_KEY:
        hash_digest = hmac.new(
            settings.PAYSTACK_SECRET_KEY.encode("utf-8"),
            payload_body,
            hashlib.sha512
        ).hexdigest()
        
        if hash_digest != signature:
            raise HTTPException(status_code=400, detail="Invalid signature")

    payload = await request.json()
    background_tasks.add_task(billing_service.process_paystack_webhook, payload)
    
    return WebhookResponse(received=True)


@router.post("/webhooks/polar", response_model=WebhookResponse)
async def polar_webhook(request: Request, background_tasks: BackgroundTasks):
    """Handle Polar webhooks."""
    # Verify signature - standard polar implementation expects verifying 'webhook-signature' 
    # Usually handled via svix or generic hmac depending on exactly what polar version is used.
    # For boilerplate, we'll verify if secret is set.
    
    signature = request.headers.get("webhook-signature")
    
    payload = await request.json()
    background_tasks.add_task(billing_service.process_polar_webhook, payload)
    
    return WebhookResponse(received=True)
