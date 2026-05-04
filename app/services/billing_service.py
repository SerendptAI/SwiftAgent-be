import httpx
import json
import time
import base64
import logging
from datetime import datetime, timezone
from typing import Dict, Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from app.core.config import settings
from app.core.billing_limits import AFRICAN_COUNTRIES, TIER_LIMITS
from app.core.database import db

logger = logging.getLogger(__name__)


class BillingService:
    def __init__(self):
        self.polar_api_url = "https://api.polar.sh/v1"

    async def create_checkout_session(self, company_id: str, tier: str, country: str, email: str) -> str:
        """
        Creates a checkout session depending on the company's country.
        African countries go to PalmPay, others to Polar.
        """
        if tier not in TIER_LIMITS:
            raise ValueError("Invalid tier selected.")

        limit_data = TIER_LIMITS[tier]
        price_ngn = limit_data["price_ngn"]

        is_african = country in AFRICAN_COUNTRIES if country else False

        if is_african:
            return await self._create_palmpay_session(company_id, tier, price_ngn, email)
        else:
            return await self._create_polar_session(company_id, tier, price_ngn, email)

    async def _create_palmpay_session(
        self, company_id: str, tier: str, price_ngn: int, email: str
    ) -> str:
        """Create a PalmPay checkout session for African countries."""
        if not settings.PALMPAY_MERCHANT_ID or not settings.PALMPAY_PRIVATE_KEY:
            logger.warning("PalmPay credentials not set. Returning dummy url.")
            return f"https://sandbox.palmpay-inc.com/checkout/dummy?company_id={company_id}"

        # PalmPay expects amount in kobo (NGN × 100)
        amount_kobo = price_ngn * 100
        order_ref = f"{company_id}-{tier}-{int(time.time())}"

        payload = {
            "merchantId": settings.PALMPAY_MERCHANT_ID,
            "amount": str(amount_kobo),
            "currency": "NGN",
            "orderId": order_ref,
            "orderTitle": f"SwiftAgent {tier.title()} Plan",
            "callbackUrl": f"{settings.FRONTEND_URL}/dashboard/billing/success",
            "notifyUrl": f"{settings.API_BASE_URL}/api/v1/billing/webhooks/palmpay",
            "buyerEmail": email,
            "extraData": json.dumps({"company_id": company_id, "tier": tier}),
        }

        # RSA-SHA256 sign the payload
        body_str = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        private_key = serialization.load_pem_private_key(
            settings.PALMPAY_PRIVATE_KEY.encode(), password=None
        )
        signature = base64.b64encode(
            private_key.sign(
                body_str.encode(),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        ).decode()

        headers = {
            "Content-Type": "application/json",
            "Signature": signature,
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{settings.PALMPAY_API_URL}/payment/v2/initiate",
                headers=headers,
                content=body_str,
            )
            response.raise_for_status()
            data = response.json()
            return data["data"]["paymentUrl"]

    async def _create_polar_session(self, company_id: str, tier: str, price_ngn: int, email: str) -> str:
        if not settings.POLAR_ACCESS_TOKEN:
            logger.warning("POLAR_ACCESS_TOKEN not set. Returning dummy url.")
            return f"https://sandbox.polar.sh/checkout/dummy?company_id={company_id}"

        headers = {
            "Authorization": f"Bearer {settings.POLAR_ACCESS_TOKEN}",
            "Content-Type": "application/json"
        }

        # Product IDs mapped to each tier from configuration
        product_map = {
            "basic": settings.POLAR_PRODUCT_BASIC,
            "pro": settings.POLAR_PRODUCT_PRO,
            "enterprise": settings.POLAR_PRODUCT_ENTERPRISE,
        }
        product_id = product_map.get(tier, product_map["basic"])

        payload = {
            "product_id": product_id,
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

    async def process_palmpay_webhook(self, payload: Dict[str, Any]) -> bool:
        """Process webhook events from PalmPay."""
        event = payload.get("notifyType")
        data = payload.get("data", {})

        if event == "ORDER_PAID":
            extra_data = json.loads(data.get("extraData", "{}"))
            company_id = extra_data.get("company_id")
            tier = extra_data.get("tier")
            order_id = data.get("orderId")

            if company_id:
                now = datetime.now(tz=timezone.utc)
                await db.companies.update_one(
                    {"id": company_id},
                    {"$set": {
                        "subscription_tier": tier,
                        "subscription_status": "active",
                        "subscription_started_at": now,
                        "billing_provider": "palmpay",
                        "subscription_id": order_id,
                    }}
                )
                from app.core.cache import company_cache
                # Try to invalidate cache by deleting the specific company keys
                keys_to_delete = [
                    key for key in list(company_cache._store.keys())
                    if key.startswith(f"company:{company_id}:")
                ]
                for key in keys_to_delete:
                    await company_cache.delete(key)

                logger.info(f"PalmPay success for company {company_id}, upgraded to {tier}. Order: {order_id}")
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
