import httpx
import logging
from datetime import datetime, timezone
from typing import Dict, Any

from app.core.config import settings
from app.core.billing_limits import TIER_LIMITS, is_african_timezone
from app.core.database import db

logger = logging.getLogger(__name__)


class BillingService:
    def __init__(self):
        self.polar_api_url = "https://api.polar.sh/v1"

    async def create_checkout_session(
        self, company_id: str, tier: str, user_timezone: str,
        email: str, client_ip: str | None = None
    ) -> str:
        """Create a Polar checkout session with region-aware pricing."""
        if tier not in TIER_LIMITS:
            raise ValueError("Invalid tier selected.")

        is_african = is_african_timezone(user_timezone)

        if not settings.POLAR_ACCESS_TOKEN:
            logger.warning("POLAR_ACCESS_TOKEN not set. Returning dummy url.")
            return f"https://sandbox.polar.sh/checkout/dummy?company_id={company_id}"

        headers = {
            "Authorization": f"Bearer {settings.POLAR_ACCESS_TOKEN}",
            "Content-Type": "application/json"
        }

        # Pick product ID based on region
        if is_african:
            product_map = {
                "basic": settings.POLAR_PRODUCT_BASIC_AF,
                "pro": settings.POLAR_PRODUCT_PRO_AF,
                "enterprise": settings.POLAR_PRODUCT_ENTERPRISE_AF,
            }
        else:
            product_map = {
                "basic": settings.POLAR_PRODUCT_BASIC_INTL,
                "pro": settings.POLAR_PRODUCT_PRO_INTL,
                "enterprise": settings.POLAR_PRODUCT_ENTERPRISE_INTL,
            }

        product_id = product_map.get(tier, product_map["basic"])

        payload = {
            "products": [product_id],
            "customer_email": email,
            "metadata": {
                "company_id": company_id,
                "tier": tier,
                "region": "african" if is_african else "international",
            },
            "success_url": f"{settings.FRONTEND_URL}/dashboard/billing/success"
        }

        # Pass client IP so Polar detects correct currency/locale
        if client_ip:
            payload["customer_ip_address"] = client_ip

        # Auto-apply 50% discount for International Basic
        if not is_african and tier == "basic" and settings.POLAR_DISCOUNT_BASIC_INTL:
            payload["discount_id"] = settings.POLAR_DISCOUNT_BASIC_INTL

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.polar_api_url}/checkouts/",
                headers=headers, json=payload
            )
            if response.status_code not in (200, 201):
                logger.error(f"Polar checkout failed: {response.text}")
                return f"https://sandbox.polar.sh/checkout/dummy?company_id={company_id}"

            data = response.json()
            return data.get("url", f"https://sandbox.polar.sh/checkout/dummy?company_id={company_id}")

    async def process_polar_webhook(self, payload: Dict[str, Any]) -> bool:
        """Process webhook events from Polar.sh"""
        event = payload.get("type")
        data = payload.get("data", {})

        if event in ("subscription.created", "subscription.updated"):
            metadata = data.get("metadata", {})
            company_id = metadata.get("company_id")
            tier = metadata.get("tier")
            sub_id = data.get("id")
            customer_id = data.get("customer_id")

            if company_id:
                now = datetime.now(tz=timezone.utc)
                await db.companies.update_one(
                    {"id": company_id},
                    {"$set": {
                        "subscription_tier": tier,
                        "subscription_status": "active",
                        "subscription_started_at": now,
                        "billing_provider": "polar",
                        "subscription_id": sub_id,
                        "customer_id": customer_id,
                    }}
                )
                from app.core.cache import company_cache
                keys_to_delete = [
                    key for key in list(company_cache._store.keys())
                    if key.startswith(f"company:{company_id}:")
                ]
                for key in keys_to_delete:
                    await company_cache.delete(key)

                logger.info(f"Polar success for company {company_id}, upgraded to {tier}. Sub: {sub_id}")
                return True

        return False


billing_service = BillingService()
