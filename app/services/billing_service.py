import httpx
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from pymongo import ReturnDocument

from app.core.config import settings
from app.core.billing_limits import TIER_LIMITS, is_african_timezone
from app.core.database import db

logger = logging.getLogger(__name__)


def _build_product_to_tier_map() -> Dict[str, str]:
    """Build a reverse map from Polar product IDs to tier names.
    
    This is essential because checkout metadata may NOT propagate to
    subscription webhooks in Polar, so we need to derive the tier from
    the product_id instead.
    """
    mapping = {}
    # African pricing products (legacy)
    if settings.POLAR_PRODUCT_BASIC_AF:
        mapping[settings.POLAR_PRODUCT_BASIC_AF] = "basic"
    if settings.POLAR_PRODUCT_PRO_AF:
        mapping[settings.POLAR_PRODUCT_PRO_AF] = "pro"
    if settings.POLAR_PRODUCT_ENTERPRISE_AF:
        mapping[settings.POLAR_PRODUCT_ENTERPRISE_AF] = "enterprise"
    
    # International pricing products (legacy)
    if settings.POLAR_PRODUCT_BASIC_INTL:
        mapping[settings.POLAR_PRODUCT_BASIC_INTL] = "basic"
    if settings.POLAR_PRODUCT_PRO_INTL:
        mapping[settings.POLAR_PRODUCT_PRO_INTL] = "pro"
    if settings.POLAR_PRODUCT_ENTERPRISE_INTL:
        mapping[settings.POLAR_PRODUCT_ENTERPRISE_INTL] = "enterprise"
    
    # Unified global pricing products (new)
    if getattr(settings, "POLAR_PRODUCT_STARTUP", None):
        mapping[settings.POLAR_PRODUCT_STARTUP] = "startup"
    if getattr(settings, "POLAR_PRODUCT_BUSINESS", None):
        mapping[settings.POLAR_PRODUCT_BUSINESS] = "business"
    if getattr(settings, "POLAR_PRODUCT_ENTERPRISE_PAYG", None):
        mapping[settings.POLAR_PRODUCT_ENTERPRISE_PAYG] = "enterprise_payg"
    return mapping


PRODUCT_TO_TIER: Dict[str, str] = _build_product_to_tier_map()


class BillingService:
    def __init__(self):
        self.polar_api_url = getattr(settings, "POLAR_API_URL", "https://api.polar.sh/v1")

    async def create_checkout_session(
        self, company_id: str, tier: str, user_timezone: str,
        email: str, client_ip: str | None = None
    ) -> str:
        """Create a Polar checkout session with region-aware pricing."""
        if tier not in TIER_LIMITS:
            raise ValueError("Invalid tier selected.")
        if tier == "none":
            raise ValueError("Cannot checkout to the 'none' tier.")

        is_african = is_african_timezone(user_timezone)

        if not settings.POLAR_ACCESS_TOKEN:
            logger.warning("POLAR_ACCESS_TOKEN not set. Returning dummy url.")
            return f"https://sandbox.polar.sh/checkout/dummy?company_id={company_id}"

        headers = {
            "Authorization": f"Bearer {settings.POLAR_ACCESS_TOKEN}",
            "Content-Type": "application/json"
        }

        # Pick product ID based on region for legacy tiers
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
            
        # Add the new unified tiers to the product map
        startup_prod = getattr(settings, "POLAR_PRODUCT_STARTUP", None)
        if startup_prod: product_map["startup"] = startup_prod
        
        business_prod = getattr(settings, "POLAR_PRODUCT_BUSINESS", None)
        if business_prod: product_map["business"] = business_prod
        
        payg_prod = getattr(settings, "POLAR_PRODUCT_ENTERPRISE_PAYG", None)
        if payg_prod: product_map["enterprise_payg"] = payg_prod

        product_id = product_map.get(tier, product_map.get("basic"))

        payload = {
            "product_id": product_id,
            "customer_email": email,
            # Link the Polar customer to our internal company_id so that
            # subscription webhooks can always identify the company, even
            # if checkout metadata does not propagate.
            "external_customer_id": company_id,
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

    async def create_customer_portal_session(self, customer_id: str) -> str:
        """Create a Polar customer portal session URL."""
        if not settings.POLAR_ACCESS_TOKEN:
            return "https://sandbox.polar.sh/purchases"

        headers = {
            "Authorization": f"Bearer {settings.POLAR_ACCESS_TOKEN}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "customer_id": customer_id
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.polar_api_url}/customer-sessions/",
                headers=headers, json=payload
            )
            if response.status_code in (200, 201):
                data = response.json()
                portal_url = data.get("customer_portal_url")
                if portal_url:
                    return portal_url
                    
            # Fallthrough for error handling
            logger.error(f"Polar customer portal failed: {response.text}")
            raise ValueError(f"Failed to load subscription portal. Polar responded with {response.status_code}")

    def _resolve_company_id(self, data: Dict[str, Any]) -> Optional[str]:
        """Extract company_id from webhook data using multiple strategies.
        
        Priority:
        1. metadata.company_id  (set during checkout, may not propagate)
        2. customer.external_id (set via external_customer_id at checkout)
        3. Look up by customer_id in our DB
        """
        # Strategy 1: Direct metadata
        metadata = data.get("metadata") or {}
        company_id = metadata.get("company_id")
        if company_id:
            logger.info(f"Resolved company_id from metadata: {company_id}")
            return company_id

        # Strategy 2: external_customer_id on the customer object
        customer = data.get("customer") or {}
        external_id = customer.get("external_id")
        if external_id:
            logger.info(f"Resolved company_id from customer.external_id: {external_id}")
            return external_id

        # Strategy 3: Reverse lookup by Polar customer_id in our DB
        # (deferred — requires async, handled in caller)
        return None

    def _resolve_tier(self, data: Dict[str, Any]) -> Optional[str]:
        """Extract the subscription tier from webhook data.
        
        Priority:
        1. metadata.tier  (set during checkout, may not propagate)
        2. Reverse-map from product_id using our configured product IDs
        """
        metadata = data.get("metadata") or {}
        tier = metadata.get("tier")
        if tier and tier in TIER_LIMITS:
            logger.info(f"Resolved tier from metadata: {tier}")
            return tier

        # Derive from product_id
        product_id = data.get("product_id")
        if product_id and product_id in PRODUCT_TO_TIER:
            tier = PRODUCT_TO_TIER[product_id]
            logger.info(f"Resolved tier from product_id {product_id}: {tier}")
            return tier

        # Try product.id (some webhook versions nest it)
        product = data.get("product") or {}
        product_id_nested = product.get("id")
        if product_id_nested and product_id_nested in PRODUCT_TO_TIER:
            tier = PRODUCT_TO_TIER[product_id_nested]
            logger.info(f"Resolved tier from product.id {product_id_nested}: {tier}")
            return tier

        logger.warning(f"Could not resolve tier. metadata={metadata}, product_id={product_id}")
        return None

    async def _resolve_company_id_with_db_fallback(
        self, data: Dict[str, Any]
    ) -> Optional[str]:
        """Full resolution including async DB fallback by customer_id and email."""
        company_id = self._resolve_company_id(data)
        if company_id:
            return company_id

        # Strategy 3: DB lookup by Polar customer_id
        polar_customer_id = data.get("customer_id")
        if polar_customer_id:
            company = await db.companies.find_one(
                {"customer_id": polar_customer_id},
                {"id": 1}
            )
            if company:
                company_id = company["id"]
                logger.info(
                    f"Resolved company_id from DB via customer_id "
                    f"{polar_customer_id}: {company_id}"
                )
                return company_id

            # Strategy 4: Fetch Customer from Polar API to get external_id
            if settings.POLAR_ACCESS_TOKEN:
                try:
                    headers = {"Authorization": f"Bearer {settings.POLAR_ACCESS_TOKEN}"}
                    async with httpx.AsyncClient() as client:
                        resp = await client.get(f"{self.polar_api_url}/customers/{polar_customer_id}", headers=headers)
                        if resp.status_code == 200:
                            customer_data = resp.json()
                            ext_id = customer_data.get("external_id")
                            if ext_id:
                                logger.info(f"Resolved company_id from fetched customer external_id: {ext_id}")
                                # Pre-link the customer to the company
                                await db.companies.update_one(
                                    {"id": ext_id},
                                    {"$set": {"customer_id": polar_customer_id}}
                                )
                                return ext_id
                except Exception as e:
                    logger.error(f"Failed to fetch customer from Polar API: {e}")

        # Strategy 5: Fallback to looking up company by the customer's email
        # This is essential for users paying via direct Polar product links
        customer_email = data.get("customer", {}).get("email") or data.get("customer_email")
        if customer_email:
            # Check contact_email first
            company = await db.companies.find_one({"contact_email": customer_email}, {"id": 1})
            if company:
                logger.info(f"Resolved company_id from DB via contact_email {customer_email}: {company['id']}")
                if polar_customer_id:
                    await db.companies.update_one({"id": company["id"]}, {"$set": {"customer_id": polar_customer_id}})
                return company["id"]
            
            # Check owner's user email
            user = await db.users.find_one({"email": customer_email}, {"user_id": 1})
            if user:
                company = await db.companies.find_one({"user_id": user["user_id"]}, {"id": 1})
                if company:
                    logger.info(f"Resolved company_id from DB via user email {customer_email}: {company['id']}")
                    if polar_customer_id:
                        await db.companies.update_one({"id": company["id"]}, {"$set": {"customer_id": polar_customer_id}})
                    return company["id"]

        logger.error(
            f"BILLING CRITICAL: Cannot resolve company_id from webhook. "
            f"metadata={data.get('metadata')}, "
            f"customer={data.get('customer')}, "
            f"customer_id={data.get('customer_id')}"
        )
        return None

    async def process_polar_webhook(self, payload: Dict[str, Any]) -> bool:
        """Process webhook events from Polar.sh"""
        event = payload.get("type")
        data = payload.get("data", {})

        logger.info(f"Processing Polar webhook: type={event}")

        if event in ("subscription.created", "subscription.updated", "subscription.active", "order.created"):
            company_id = await self._resolve_company_id_with_db_fallback(data)
            tier = self._resolve_tier(data)
            sub_id = data.get("id")
            customer_id = data.get("customer_id")

            if not company_id:
                logger.error(
                    f"BILLING BUG: Received {event} but could not resolve "
                    f"company_id. Subscription {sub_id} is ORPHANED. "
                    f"Full data keys: {list(data.keys())}"
                )
                return False

            if not tier:
                logger.error(
                    f"BILLING BUG: Received {event} for company {company_id} "
                    f"but could not resolve tier. Sub: {sub_id}. "
                    f"Defaulting to basic."
                )
                tier = "basic"

            now = datetime.now(tz=timezone.utc)
            old_company = await db.companies.find_one({"id": company_id})

            if not old_company:
                logger.error(
                    f"BILLING BUG: find_one matched 0 documents for "
                    f"company_id={company_id}. Company may not exist!"
                )
                return False

            old_tier = old_company.get("subscription_tier")

            update_data = {
                "subscription_tier": tier,
                "subscription_status": "active",
                "billing_provider": "polar",
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

            logger.info(
                f"Polar {event} SUCCESS: company={company_id}, "
                f"tier={tier}, sub={sub_id}, customer={customer_id}"
            )

            # Notify dashboard users about the upgrade
            if old_tier != tier:
                import asyncio
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

            return True

        elif event in ("subscription.canceled", "subscription.revoked"):
            company_id = await self._resolve_company_id_with_db_fallback(data)
            if not company_id:
                logger.error(
                    f"BILLING BUG: Received {event} but could not resolve "
                    f"company_id. Full data keys: {list(data.keys())}"
                )
                return False

            await db.companies.update_one(
                {"id": company_id},
                {"$set": {
                    "subscription_status": "canceled",
                }}
            )
            from app.core.cache import company_cache
            keys_to_delete = [
                key for key in list(company_cache._store.keys())
                if key.startswith(f"company:{company_id}:")
            ]
            for key in keys_to_delete:
                await company_cache.delete(key)

            logger.info(f"Polar {event}: company={company_id}")
            return True

        elif event in ("customer.created", "customer.updated"):
            ext_id = data.get("external_id")
            cust_id = data.get("id")
            if ext_id and cust_id:
                logger.info(f"Polar {event}: linking customer_id={cust_id} to company={ext_id}")
                await db.companies.update_one(
                    {"id": ext_id},
                    {"$set": {"customer_id": cust_id}}
                )
            return True

        else:
            logger.info(f"Polar webhook ignored: unhandled event type '{event}'")

        return False

    async def ingest_meter_event(self, company_id: str, event_name: str) -> None:
        """Send a metered usage event to Polar's ingest API."""
        if not settings.POLAR_ACCESS_TOKEN:
            logger.info(f"Skipping meter ingestion for {event_name} (No Polar Token).")
            return
            
        # --- Check Included Allowances ---
        company = await db.companies.find_one({"id": company_id})
        if not company:
            return
            
        from app.core.plan_enforcement import get_active_tier, get_tier_limits
        tier = get_active_tier(company)
        
        # Only enterprise tiers use Pay-As-You-Go metering in Polar
        if tier not in ("enterprise", "enterprise_payg"):
            return
            
        limits = get_tier_limits(tier)
        should_bill = False
        
        if event_name == "document_added":
            max_docs = limits.get("documents_limit", 0)
            if max_docs != -1:
                current_docs = await db.knowledge_sources.count_documents({"company_id": company_id})
                if current_docs > max_docs:
                    should_bill = True
        
        elif event_name == "member_added":
            max_members = limits.get("members_per_company", 0)
            if max_members != -1:
                active_members = len(company.get("members", []))
                now = datetime.now(tz=timezone.utc)
                active_invites = sum(
                    1 for inv in company.get("pending_invites", [])
                    if (now - (inv.get("invited_at", now).replace(tzinfo=timezone.utc) if inv.get("invited_at", now).tzinfo is None else inv.get("invited_at", now))).days < 10
                )
                if (active_members + active_invites) > max_members:
                    should_bill = True
                    
        elif event_name == "stroll_used":
            max_strolls = limits.get("strolls_per_month", 0)
            if max_strolls != -1:
                now = datetime.now(tz=timezone.utc)
                month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                current_strolls = await db.stroll_versions.count_documents({
                    "company_id": company_id,
                    "timestamp": {"$gte": month_start},
                    "status": "success"
                })
                if current_strolls > max_strolls:
                    should_bill = True
        else:
            should_bill = True
            
        if not should_bill:
            logger.debug(f"Meter event '{event_name}' for {company_id} is within included free limits. Not sending to Polar.")
            return
        # ---------------------------------

        headers = {
            "Authorization": f"Bearer {settings.POLAR_ACCESS_TOKEN}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "events": [
                {
                    "name": event_name,
                    "external_customer_id": company_id
                }
            ]
        }

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self.polar_api_url}/events/ingest",
                    headers=headers,
                    json=payload
                )
                if resp.status_code not in (200, 201, 202, 204):
                    logger.error(f"Failed to ingest Polar meter event '{event_name}' for company {company_id}: {resp.text}")
                else:
                    logger.debug(f"Successfully ingested Polar meter event '{event_name}' for company {company_id}.")
        except Exception as e:
            logger.error(f"Exception while ingesting Polar meter event '{event_name}': {e}")


billing_service = BillingService()
