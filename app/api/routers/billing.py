import logging
from fastapi import APIRouter, Depends, HTTPException, Request, Query, BackgroundTasks
import hmac, hashlib, base64

from app.core.auth import get_current_user
from app.core.database import db
from app.core.config import settings
from app.core.plan_enforcement import get_usage_summary
from app.core.billing_limits import TIER_LIMITS, is_african_timezone
from app.models.billing_models import CheckoutSessionRequest, CheckoutSessionResponse, WebhookResponse, PortalSessionResponse
from app.services.billing_service import billing_service
from app.services.company_service import get_company

router = APIRouter(tags=["Billing"])
logger = logging.getLogger(__name__)


@router.get("/plans")
async def get_billing_plans(timezone: str = Query("", description="User timezone e.g. Africa/Lagos")):
    """Retrieve available billing plans with region-specific pricing."""
    is_af = is_african_timezone(timezone)
    plans = {}
    for tier_key, limits in TIER_LIMITS.items():
        plan = {k: v for k, v in limits.items()}
        
        # Add companies_limit (translating -1 to None for unlimited)
        cpu = limits.get("companies_per_user")
        plan["companies_limit"] = None if cpu == -1 else cpu

        plan["price_usd"] = limits["price_usd_af"] if is_af else limits.get("price_usd_intl_discounted", limits["price_usd_intl"])
        if not is_af and "price_usd_intl_discounted" in limits:
            plan["price_usd_original"] = limits["price_usd_intl"]
        plan["region"] = "african" if is_af else "international"
        if is_af and "trial_months_af" in limits:
            plan["trial_months"] = limits["trial_months_af"]
        plans[tier_key] = plan
    return plans


@router.get("/{company_id}/status")
async def get_billing_status(company_id: str, current_user: dict = Depends(get_current_user)):
    """Retrieve the company's current subscription status and usage."""
    user_id = current_user["user_id"]
    company = await get_company(company_id, user_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    summary = await get_usage_summary(company)
    return summary


@router.post("/checkout", response_model=CheckoutSessionResponse)
async def create_checkout_session(
    body: CheckoutSessionRequest,
    request: Request,
    current_user: dict = Depends(get_current_user)
):
    """Generate a Polar checkout session with region-aware pricing."""
    company = await db.companies.find_one(
        {"id": body.company_id, "user_id": current_user["user_id"]}
    )
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    email = company.get("contact_email") or current_user.get("email")

    # Get client IP (behind reverse proxy or direct)
    client_ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if not client_ip and request.client:
        client_ip = request.client.host

    try:
        checkout_url = await billing_service.create_checkout_session(
            company_id=body.company_id,
            tier=body.tier,
            user_timezone=body.user_timezone,
            email=email,
            client_ip=client_ip,
        )
        return CheckoutSessionResponse(checkout_url=checkout_url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Checkout session creation failed: {e}")
        raise HTTPException(
            status_code=500,
            detail="Unable to start checkout. Please try again or contact support."
        )


@router.post("/{company_id}/portal", response_model=PortalSessionResponse)
async def create_portal_session(
    company_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Generate a Polar customer portal URL so the user can manage/cancel their subscription."""
    company = await db.companies.find_one(
        {"id": company_id, "user_id": current_user["user_id"]}
    )
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    customer_id = company.get("customer_id")
    if not customer_id:
        raise HTTPException(status_code=400, detail="This company does not have an active billing customer ID.")

    try:
        portal_url = await billing_service.create_customer_portal_session(customer_id)
        return PortalSessionResponse(portal_url=portal_url)
    except Exception as e:
        logger.error(f"Portal session creation failed: {e}")
        raise HTTPException(
            status_code=500,
            detail="Unable to open billing portal. Please try again or contact support."
        )


@router.post("/webhooks/polar", response_model=WebhookResponse)
async def polar_webhook(request: Request, background_tasks: BackgroundTasks):
    """Handle Polar webhooks."""
    webhook_id = request.headers.get("webhook-id")
    webhook_timestamp = request.headers.get("webhook-timestamp")
    webhook_signature = request.headers.get("webhook-signature")

    if not webhook_id or not webhook_timestamp or not webhook_signature:
        raise HTTPException(status_code=400, detail="Missing webhook headers")
        
    webhook_secret = settings.POLAR_WEBHOOK_SECRET
    if not webhook_secret:
        raise HTTPException(status_code=500, detail="Payment verification service is temporarily unavailable.")

    try:
        secret = webhook_secret.removeprefix("whsec_")
        secret_bytes = base64.b64decode(secret)
    except Exception:
        raise HTTPException(status_code=500, detail="Invalid server webhook secret")

    payload_body = await request.body()
    to_sign = f"{webhook_id}.{webhook_timestamp}.{payload_body.decode('utf-8')}".encode("utf-8")

    computed_signature = hmac.new(secret_bytes, to_sign, hashlib.sha256).digest()
    computed_signature_b64 = base64.b64encode(computed_signature).decode("utf-8")

    passed = False
    for sig in webhook_signature.split(" "):
        parts = sig.split(",")
        if len(parts) == 2 and parts[0] == "v1" and hmac.compare_digest(parts[1], computed_signature_b64):
            passed = True
            break

    if not passed:
        raise HTTPException(status_code=400, detail="Invalid signature")

    payload = await request.json()
    background_tasks.add_task(billing_service.process_polar_webhook, payload)
    return WebhookResponse(received=True)
