import httpx
from datetime import datetime, timezone
from app.core.config import settings
from app.core.billing_limits import AFRICAN_COUNTRIES, TIER_LIMITS
from app.core.database import db
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

class BillingService:
    def __init__(self):
        self.paystack_api_url = "https://api.paystack.co"
        self.polar_api_url = "https://api.polar.sh/v1" # or latest version

    async def create_checkout_session(self, company_id: str, tier: str, country: str, email: str) -> str:
        """
        Creates a checkout session depending on the company's country.
        African countries go to Paystack, others to Polar.
        """
        if tier not in TIER_LIMITS:
            raise ValueError("Invalid tier selected.")
        
        limit_data = TIER_LIMITS[tier]
        price_ngn = limit_data["price_ngn"]
        
        is_african = country in AFRICAN_COUNTRIES if country else False

        if is_african:
            return await self._create_paystack_session(company_id, tier, price_ngn, email)
        else:
            return await self._create_polar_session(company_id, tier, price_ngn, email)

    async def _create_paystack_session(self, company_id: str, tier: str, price_ngn: int, email: str) -> str:
        if not settings.PAYSTACK_SECRET_KEY:
            logger.warning("PAYSTACK_SECRET_KEY not set. Returning dummy url.")
            return f"https://sandbox.paystack.com/checkout/dummy?company_id={company_id}"

        headers = {
            "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
            "Content-Type": "application/json"
        }
        
        # Paystack expects amount in kobo
        amount_kobo = price_ngn * 100
        
        payload = {
            "email": email,
            "amount": amount_kobo,
            "metadata": {
                "company_id": company_id,
                "tier": tier
            },
            "callback_url": f"{settings.FRONTEND_URL}/dashboard/billing/success"
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(f"{self.paystack_api_url}/transaction/initialize", headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
            return data["data"]["authorization_url"]

    async def _create_polar_session(self, company_id: str, tier: str, price_ngn: int, email: str) -> str:
        if not settings.POLAR_ACCESS_TOKEN:
            logger.warning("POLAR_ACCESS_TOKEN not set. Returning dummy url.")
            return f"https://sandbox.polar.sh/checkout/dummy?company_id={company_id}"
            
        headers = {
            "Authorization": f"Bearer {settings.POLAR_ACCESS_TOKEN}",
            "Content-Type": "application/json"
        }
        
        # Here we would map the tier to the specific Polar Product ID in practice.
        # This is boilerplate to await product IDs or generic checkout generation.
        # Let's assume there's a custom or standard price product mapping.
        
        payload = {
            # Typically expects a productId or priceId, and customer details.
            # "product_id": ...,
            "customer_email": email,
            "metadata": {
                "company_id": company_id,
                "tier": tier
            },
            "success_url": f"{settings.FRONTEND_URL}/dashboard/billing/success"
        }
        
        async with httpx.AsyncClient() as client:
            # Note: adjust the Polar endpoint to match actual Polar checkout api v1 (usually /checkouts)
            response = await client.post(f"{self.polar_api_url}/checkouts", headers=headers, json=payload)
            if response.status_code != 200:
                logger.error(f"Polar checkout failed: {response.text}")
                # return dummy for development continuity if failing
                return f"https://sandbox.polar.sh/checkout/dummy?company_id={company_id}"
                
            data = response.json()
            return data.get("url", f"https://sandbox.polar.sh/checkout/dummy?company_id={company_id}")

    async def process_paystack_webhook(self, payload: Dict[str, Any]) -> bool:
        """Process webhook events from Paystack"""
        # Note: we should verify x-paystack-signature in the router wrapper
        event = payload.get("event")
        data = payload.get("data", {})
        
        if event == "charge.success":
            metadata = data.get("metadata", {})
            company_id = metadata.get("company_id")
            tier = metadata.get("tier")
            reference = data.get("reference")
            customer_code = data.get("customer", {}).get("customer_code")
            
            if company_id:
                now = datetime.now(tz=timezone.utc)
                await db.companies.update_one(
                    {"id": company_id},
                    {"$set": {
                        "subscription_tier": tier,
                        "subscription_status": "active",
                        "subscription_started_at": now,
                        "billing_provider": "paystack",
                        "subscription_id": reference,
                    }}
                )
                from app.core.cache import company_cache
                # Try to invalidate cache by deleting the specific company keys
                keys_to_delete = []
                for key in list(company_cache._store.keys()):
                    if key.startswith(f"company:{company_id}:"):
                        keys_to_delete.append(key)
                for key in keys_to_delete:
                    await company_cache.delete(key)

                logger.info(f"Paystack success for company {company_id}, upgraded to {tier}. Ref: {reference}")
                return True
                
        return False

    async def process_polar_webhook(self, payload: Dict[str, Any]) -> bool:
        """Process webhook events from Polar.sh"""
        event = payload.get("type")
        data = payload.get("data", {})
        
        # Examples of polar events: subscription.created, subscription.updated
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
                keys_to_delete = []
                for key in list(company_cache._store.keys()):
                    if key.startswith(f"company:{company_id}:"):
                        keys_to_delete.append(key)
                for key in keys_to_delete:
                    await company_cache.delete(key)

                logger.info(f"Polar success for company {company_id}, upgraded to {tier}. Sub: {sub_id}")
                return True
                
        return False

billing_service = BillingService()
