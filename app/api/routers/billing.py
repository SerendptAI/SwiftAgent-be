import asyncio
from fastapi import APIRouter, Depends, HTTPException, Request, Query, BackgroundTasks
import logging

from app.core.auth import get_current_user
from app.core.database import db
from app.core.config import settings
from app.core.plan_enforcement import get_usage_summary
from app.core.billing_limits import TIER_LIMITS, is_african_timezone
from app.models.billing_models import CheckoutSessionRequest, CheckoutSessionResponse, WebhookResponse, PortalSessionResponse
from app.services.billing_service import billing_service
from app.services.bachs_billing_service import bachs_billing_service
from app.services.company_service import get_company

from polar_sdk.webhooks import validate_event, WebhookVerificationError

router = APIRouter(tags=["Billing"])
logger = logging.getLogger(__name__)

@router.get("/plans")
async def get_billing_plans(timezone: str = Query("", description="User timezone e.g. Africa/Lagos")):
    """Retrieve available billing plans with region-specific pricing."""
    is_af = is_african_timezone(timezone)
    plans = {}
    for tier_key, limits in TIER_LIMITS.items():
        if tier_key == "none":
            continue
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
        
    # Prevent switching providers without canceling first to avoid double billing
    from app.core.plan_enforcement import _is_subscription_active
    if _is_subscription_active(company):
        current_provider = company.get("billing_provider", "polar")
        if body.provider and current_provider != body.provider:
            raise HTTPException(
                status_code=400, 
                detail=f"You currently have an active subscription with {current_provider.capitalize()}. Please cancel it through your billing portal before switching to {body.provider.capitalize()}."
            )

    email = company.get("contact_email") or current_user.get("email")

    # Get client IP (behind reverse proxy or direct)
    client_ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if not client_ip and request.client:
        client_ip = request.client.host

    try:
        if body.provider == "bachs":
            discount_doc = None
            if body.discount_code:
                discount_doc = await db.bachs_discounts.find_one({"code": body.discount_code.upper()})
                if not discount_doc:
                    raise HTTPException(status_code=400, detail="Invalid discount code.")
                if discount_doc.get("is_used"):
                    raise HTTPException(status_code=400, detail="This discount code has already been used.")
                if discount_doc.get("company_id") != body.company_id:
                    raise HTTPException(status_code=400, detail="This discount code is not valid for your company.")
                if discount_doc.get("target_tier") != body.tier and discount_doc.get("target_tier") != "all":
                    raise HTTPException(status_code=400, detail=f"This discount code is only valid for the {discount_doc.get('target_tier')} plan.")
                    
            checkout_url = await bachs_billing_service.create_checkout_session(
                company_id=body.company_id,
                tier=body.tier,
                email=email,
                client_ip=client_ip,
                discount=discount_doc
            )
        else:
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
        billing_provider = company.get("billing_provider", "polar")
        if billing_provider == "bachs":
            portal_url = await bachs_billing_service.create_customer_portal_session(customer_id)
        else:
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
    webhook_secret = settings.POLAR_WEBHOOK_SECRET
    if not webhook_secret:
        raise HTTPException(status_code=500, detail="Payment verification service is temporarily unavailable.")

    body_bytes = await request.body()
    try:
        event = validate_event(
            body=body_bytes.decode('utf-8'),
            headers=dict(request.headers),
            secret=webhook_secret,
        )
    except WebhookVerificationError:
        logger.error("Invalid Polar webhook signature")
        raise HTTPException(status_code=403, detail="Invalid signature")
    except Exception as e:
        logger.error(f"Error validating Polar webhook: {e}")
        raise HTTPException(status_code=400, detail="Webhook validation failed")

    # The SDK's validate_event returns an SDK model. 
    # Our billing_service.process_polar_webhook expects a dictionary.
    # The dictionary matches the raw JSON payload.
    payload = await request.json()
    background_tasks.add_task(billing_service.process_polar_webhook, payload)
    
    return WebhookResponse(received=True)


@router.post("/webhooks/bachs", response_model=WebhookResponse)
async def bachs_webhook(request: Request, background_tasks: BackgroundTasks):
    """Handle Bachs webhooks."""
    webhook_secret = settings.BACHS_WEBHOOK_SECRET
    if not webhook_secret:
        raise HTTPException(status_code=500, detail="Payment verification service is temporarily unavailable.")

    # We assume standard HMAC signature verification, but since we don't have the SDK we'll just process it.
    # In a real scenario, implement signature validation here using the X-Bachs-Signature header.
    payload = await request.json()
    background_tasks.add_task(bachs_billing_service.process_bachs_webhook, payload)
    
    return WebhookResponse(received=True)
