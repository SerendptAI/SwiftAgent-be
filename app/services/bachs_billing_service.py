import httpx
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from pymongo import ReturnDocument

from app.core.config import settings
from app.core.database import db

logger = logging.getLogger(__name__)


def _build_product_to_tier_map() -> Dict[str, str]:
    mapping = {}
    if getattr(settings, "BACHS_PRODUCT_STARTUP", None):
        mapping[settings.BACHS_PRODUCT_STARTUP] = "startup"
    if getattr(settings, "BACHS_PRODUCT_BUSINESS", None):
        mapping[settings.BACHS_PRODUCT_BUSINESS] = "business"
    if getattr(settings, "BACHS_PRODUCT_ENTERPRISE", None):
        mapping[settings.BACHS_PRODUCT_ENTERPRISE] = "enterprise"
    return mapping


PRODUCT_TO_TIER: Dict[str, str] = _build_product_to_tier_map()


class BachsBillingService:
    def __init__(self):
        self.api_url = getattr(settings, "BACHS_API_URL", "https://api.bachs.io/v1")

    async def create_checkout_session(
        self, company_id: str, tier: str, email: str, client_ip: str | None = None, discount: dict | None = None
    ) -> str:
        """Create a Bachs checkout session."""
        if tier == "none":
            raise ValueError("Cannot checkout to the 'none' tier.")

        if not settings.BACHS_API_KEY:
            logger.warning("BACHS_API_KEY not set. Returning dummy url.")
            return f"https://sandbox.bachs.io/checkout/dummy?company_id={company_id}"

        headers = {
            "Authorization": f"Bearer {settings.BACHS_API_KEY}",
            "Content-Type": "application/json"
        }

        product_map = {
            "startup": settings.BACHS_PRODUCT_STARTUP,
            "business": settings.BACHS_PRODUCT_BUSINESS,
            "enterprise": settings.BACHS_PRODUCT_ENTERPRISE,
            # Handle aliases if needed
            "enterprise_payg": settings.BACHS_PRODUCT_ENTERPRISE,
        }

        product_id = product_map.get(tier, product_map.get("startup"))

        payload = {
            "customer": {
                "email": email,
                "name": company_id  # Using company_id as name placeholder if name isn't provided
            },
            "product_cart": [
                {
                    "product_id": product_id,
                    "quantity": 1
                }
            ],
            "metadata": {
                "company_id": company_id,
                "tier": tier,
            },
            "success_url": f"{settings.FRONTEND_URL}/dashboard/billing/success",
            "reference": f"checkout_{company_id}_{tier}_{int(datetime.now().timestamp())}"
        }

        # Apply Ad-hoc Pricing if a discount is provided
        if discount:
            pct = discount.get("percentage", 0)
            if pct >= 100:
                payload["product_cart"][0]["pricing"] = {"price_type": "free"}
            elif pct > 0:
                # Base prices for calculation
                base_prices = {"startup": 20.00, "business": 10.00, "enterprise": 100.00}
                original_price = base_prices.get(tier, 20.00)
                new_price = original_price * (1 - (pct / 100))
                payload["product_cart"][0]["pricing"] = {
                    "price_type": "fixed",
                    "amount": f"{new_price:.2f}"
                }
            # Attach discount_code to metadata for webhook consumption
            payload["metadata"]["discount_code"] = discount.get("code")

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.api_url}/checkout-sessions",
                    headers=headers, json=payload
                )
                if response.status_code not in (200, 201):
                    logger.error(f"Bachs checkout failed: {response.text}")
                    return f"https://sandbox.bachs.io/checkout/dummy?company_id={company_id}"

                data = response.json()
                # Try a few common response fields
                return data.get("url") or data.get("checkout_url") or data.get("data", {}).get("url", f"https://sandbox.bachs.io/checkout/dummy?company_id={company_id}")
        except httpx.RequestError as e:
            logger.error(f"Bachs API connection failed: {e}")
            return f"https://sandbox.bachs.io/checkout/dummy?company_id={company_id}"
        except Exception as e:
            logger.error(f"Bachs checkout error: {e}")
            return f"https://sandbox.bachs.io/checkout/dummy?company_id={company_id}"

    async def create_customer_portal_session(self, customer_id: str) -> str:
        """Create a Bachs customer portal session URL."""
        if not settings.BACHS_API_KEY:
            return "https://sandbox.bachs.io/portal"

        headers = {
            "Authorization": f"Bearer {settings.BACHS_API_KEY}",
            "Content-Type": "application/json"
        }
        
        # No payload for portal sessions, ID goes in path
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.api_url}/customers/{customer_id}/portal-sessions",
                headers=headers
            )
            if response.status_code in (200, 201):
                data = response.json()
                portal_url = data.get("customer_portal_url") or data.get("url") or data.get("data", {}).get("url")
                if portal_url:
                    return portal_url
                    
            logger.error(f"Bachs customer portal failed: {response.text}")
            raise ValueError(f"Failed to load subscription portal. Bachs responded with {response.status_code}")

    def _resolve_company_id(self, data: Dict[str, Any]) -> Optional[str]:
        metadata = data.get("metadata") or {}
        company_id = metadata.get("company_id")
        if company_id:
            return company_id

        customer = data.get("customer") or {}
        external_id = customer.get("external_id")
        if external_id:
            return external_id

        return None

    def _resolve_tier(self, data: Dict[str, Any]) -> Optional[str]:
        metadata = data.get("metadata") or {}
        tier = metadata.get("tier")
        if tier:
            return tier

        product_id = data.get("product_id")
        if product_id and product_id in PRODUCT_TO_TIER:
            return PRODUCT_TO_TIER[product_id]

        product = data.get("product") or {}
        product_id_nested = product.get("id")
        if product_id_nested and product_id_nested in PRODUCT_TO_TIER:
            return PRODUCT_TO_TIER[product_id_nested]

        return None

    async def _resolve_company_id_with_db_fallback(self, data: Dict[str, Any]) -> Optional[str]:
        company_id = self._resolve_company_id(data)
        if company_id:
            return company_id

        bachs_customer_id = data.get("customer_id")
        if bachs_customer_id:
            company = await db.companies.find_one(
                {"customer_id": bachs_customer_id},
                {"id": 1}
            )
            if company:
                return company["id"]
                
        # Email fallback
        customer_email = data.get("customer", {}).get("email") or data.get("customer_email")
        if customer_email:
            company = await db.companies.find_one({"contact_email": customer_email}, {"id": 1})
            if company:
                if bachs_customer_id:
                    await db.companies.update_one({"id": company["id"]}, {"$set": {"customer_id": bachs_customer_id}})
                return company["id"]
            
            user = await db.users.find_one({"email": customer_email}, {"user_id": 1})
            if user:
                company = await db.companies.find_one({"user_id": user["user_id"]}, {"id": 1})
                if company:
                    if bachs_customer_id:
                        await db.companies.update_one({"id": company["id"]}, {"$set": {"customer_id": bachs_customer_id}})
                    return company["id"]

        logger.error(f"Cannot resolve company_id from Bachs webhook. data={data}")
        return None

    async def process_bachs_webhook(self, payload: Dict[str, Any]) -> bool:
        """Process webhook events from Bachs"""
        event = payload.get("type")
        data = payload.get("data", {})

        logger.info(f"Processing Bachs webhook: type={event}")

        if event in ("subscription.created", "subscription.updated", "subscription.active", "order.created"):
            company_id = await self._resolve_company_id_with_db_fallback(data)
            tier = self._resolve_tier(data)
            sub_id = data.get("id")
            customer_id = data.get("customer_id")

            if not company_id:
                logger.error(f"Received {event} but could not resolve company_id for Bachs.")
                return False

            if not tier:
                tier = "startup"

            now = datetime.now(tz=timezone.utc)
            updates = {
                "subscription_tier": tier,
                "subscription_status": "active",
                "subscription_started_at": now,
                "billing_provider": "bachs",
                "subscription_id": sub_id,
                "customer_id": customer_id,
            }
            
            current_period_end = data.get("current_period_end")
            if current_period_end:
                if isinstance(current_period_end, str) and current_period_end.endswith("Z"):
                    current_period_end = current_period_end[:-1] + "+00:00"
                try:
                    updates["subscription_expires_at"] = datetime.fromisoformat(current_period_end)
                except Exception:
                    pass

            old_company = await db.companies.find_one_and_update(
                {"id": company_id},
                {"$set": updates},
                return_document=ReturnDocument.BEFORE
            )

            if not old_company:
                return False

            old_tier = old_company.get("subscription_tier")

            update_data = {
                "subscription_tier": tier,
                "subscription_status": "active",
                "billing_provider": "bachs",
                "subscription_id": sub_id,
                "customer_id": customer_id,
            }

            if old_tier != tier or not old_company.get("subscription_started_at"):
                update_data["subscription_started_at"] = now

            await db.companies.update_one(
                {"id": company_id},
                {"$set": update_data}
            )

            from app.core.cache import company_cache
            keys_to_delete = [
                key for key in list(company_cache._store.keys())
                if key.startswith(f"company:{company_id}:")
            ]
            for key in keys_to_delete:
                await company_cache.delete(key)

            if old_tier and old_tier != tier:
                import asyncio
                
                tier_order = {
                    "none": 0, "startup": 1, "business": 2, "enterprise": 3, "enterprise_payg": 3,
                }
                old_rank = tier_order.get(old_tier, 0)
                new_rank = tier_order.get(tier, 0)

                if new_rank < old_rank:
                    from app.services.billing_service import billing_service
                    asyncio.create_task(billing_service._enforce_downgrade(company_id, tier))
                else:
                    from app.services import notification_service
                    asyncio.create_task(
                        notification_service.notify_company(
                            company_id=company_id,
                            title="🎉 Plan Upgraded!",
                            body=f"Congratulations! Your company plan has been upgraded to {tier.capitalize()}.",
                            type="plan_upgrade",
                            data={"tier": tier, "subscription_id": sub_id}
                        )
                    )

            # Cycle renewal handled by standard downgrade/upgrade logic for Bachs
            
            # Check for discount_code and burn it
            metadata = data.get("metadata") or {}
            discount_code = metadata.get("discount_code")
            if discount_code:
                await db.bachs_discounts.update_one(
                    {"code": discount_code},
                    {
                        "$set": {
                            "is_used": True,
                            "used_at": now,
                            "subscription_id": sub_id
                        }
                    }
                )
                
            return True

        elif event in ("subscription.canceled", "subscription.revoked"):
            company_id = await self._resolve_company_id_with_db_fallback(data)
            if not company_id:
                return False

            updates = {
                "subscription_status": "canceled",
            }
            current_period_end = data.get("current_period_end")
            if current_period_end:
                if isinstance(current_period_end, str) and current_period_end.endswith("Z"):
                    current_period_end = current_period_end[:-1] + "+00:00"
                try:
                    updates["subscription_expires_at"] = datetime.fromisoformat(current_period_end)
                except Exception:
                    pass

            await db.companies.update_one(
                {"id": company_id},
                {"$set": updates}
            )
            from app.core.cache import company_cache
            keys_to_delete = [
                key for key in list(company_cache._store.keys())
                if key.startswith(f"company:{company_id}:")
            ]
            for key in keys_to_delete:
                await company_cache.delete(key)

            return True

        return False

bachs_billing_service = BachsBillingService()
