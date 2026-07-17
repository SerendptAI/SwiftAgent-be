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

            # Detect tier changes: downgrade enforcement or upgrade notification
            if old_tier and old_tier != tier:
                import asyncio
                from app.core.billing_limits import TIER_LIMITS

                tier_order = {
                    "none": 0, "business": 1, "basic": 1,
                    "startup": 2, "pro": 2,
                    "enterprise": 3, "enterprise_payg": 3,
                }
                old_rank = tier_order.get(old_tier, 0)
                new_rank = tier_order.get(tier, 0)

                if new_rank < old_rank:
                    # Downgrade — auto-archive excess resources
                    asyncio.create_task(
                        self._enforce_downgrade(company_id, tier)
                    )
                    logger.info(
                        f"Downgrade detected for {company_id}: "
                        f"{old_tier} → {tier}. Archiving excess resources."
                    )
                else:
                    # Upgrade notification
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

            # Detect billing cycle renewal for PAYG tiers
            if tier in ("enterprise", "enterprise_payg"):
                import asyncio
                asyncio.create_task(
                    self._handle_cycle_renewal(company_id, tier, data)
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
        """Send a metered usage event to Polar via the reliable queue.

        This method implements high-water mark tracking to prevent
        double-billing when users churn (delete + re-add) resources
        within the same billing cycle.
        """
        if not settings.POLAR_ACCESS_TOKEN:
            logger.info(f"Skipping meter ingestion for {event_name} (No Polar Token).")
            return

        company = await db.companies.find_one({"id": company_id})
        if not company:
            return

        from app.core.plan_enforcement import get_active_tier
        from app.core.billing_limits import get_tier_limits
        tier = get_active_tier(company)

        # Only enterprise tiers use Pay-As-You-Go metering in Polar
        if tier not in ("enterprise", "enterprise_payg"):
            return

        limits = get_tier_limits(tier)
        now = datetime.now(tz=timezone.utc)

        # --- High-Water Mark Logic ---
        peaks = company.get("metered_peaks") or {}
        period_start = peaks.get("period_start")

        # If no period or the period is stale (new billing cycle), reset peaks
        if not period_start or period_start < now.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        ):
            peaks = {
                "documents": limits.get("documents_limit", 0),
                "members": limits.get("members_per_company", 0),
                "strolls": limits.get("strolls_per_month", 0),
                "period_start": now.replace(
                    day=1, hour=0, minute=0, second=0, microsecond=0
                ),
            }
            await db.companies.update_one(
                {"id": company_id},
                {"$set": {"metered_peaks": peaks}},
            )

        from app.services.meter_queue_service import enqueue_meter_event

        if event_name == "document_added":
            included = limits.get("documents_limit", 0)
            if included == -1:
                return
            current = await db.knowledge_sources.count_documents(
                {"company_id": company_id}
            )
            if current <= included:
                return
            prev_peak = peaks.get("documents", included)
            if current <= prev_peak:
                logger.debug(
                    f"Document count {current} <= peak {prev_peak} for "
                    f"{company_id}. Churn detected, skipping billing."
                )
                return
            # Bill only for the NEW peak units above the old peak
            events_to_bill = current - prev_peak
            for _ in range(events_to_bill):
                await enqueue_meter_event(company_id, event_name)
            await db.companies.update_one(
                {"id": company_id},
                {"$set": {"metered_peaks.documents": current}},
            )

        elif event_name == "member_added":
            included = limits.get("members_per_company", 0)
            if included == -1:
                return
            active_members = len(company.get("members", []))
            active_invites = sum(
                1
                for inv in company.get("pending_invites", [])
                if (
                    now
                    - (
                        inv.get("invited_at", now).replace(tzinfo=timezone.utc)
                        if inv.get("invited_at", now).tzinfo is None
                        else inv.get("invited_at", now)
                    )
                ).days
                < 10
            )
            current = active_members + active_invites
            if current <= included:
                return
            prev_peak = peaks.get("members", included)
            if current <= prev_peak:
                logger.debug(
                    f"Member count {current} <= peak {prev_peak} for "
                    f"{company_id}. Churn detected, skipping billing."
                )
                return
            events_to_bill = current - prev_peak
            for _ in range(events_to_bill):
                await enqueue_meter_event(company_id, event_name)
            await db.companies.update_one(
                {"id": company_id},
                {"$set": {"metered_peaks.members": current}},
            )

        elif event_name == "stroll_used":
            included = limits.get("strolls_per_month", 0)
            if included == -1:
                return
            month_start = now.replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            )
            current = await db.stroll_versions.count_documents(
                {
                    "company_id": company_id,
                    "timestamp": {"$gte": month_start},
                    "status": "success",
                }
            )
            if current <= included:
                return
            # Strolls are monotonic (can't delete a stroll), so just bill 1
            await enqueue_meter_event(company_id, event_name)

        else:
            # Unknown event type — bill it directly as a safety net
            await enqueue_meter_event(company_id, event_name)

    # ── Cycle Renewal ────────────────────────────────────────────────────

    async def _handle_cycle_renewal(
        self, company_id: str, tier: str, data: dict
    ) -> None:
        """Detect a new billing cycle and re-sync stateful overage.

        Called from process_polar_webhook when a subscription.active or
        subscription.updated event arrives.  If Polar's current_period_start
        has advanced past the company's stored period, we reset the
        high-water marks and immediately re-bill any still-active overage
        for the new cycle.
        """
        from app.core.billing_limits import get_tier_limits
        from app.services.meter_queue_service import enqueue_meter_event

        new_period_start_str = data.get("current_period_start")
        if not new_period_start_str:
            return

        if isinstance(new_period_start_str, str):
            new_period_start = datetime.fromisoformat(
                new_period_start_str.replace("Z", "+00:00")
            )
        else:
            new_period_start = new_period_start_str

        if new_period_start.tzinfo is None:
            new_period_start = new_period_start.replace(tzinfo=timezone.utc)

        company = await db.companies.find_one({"id": company_id})
        if not company:
            return

        peaks = company.get("metered_peaks") or {}
        old_period = peaks.get("period_start")

        if old_period and new_period_start <= old_period:
            # Same cycle — nothing to do
            return

        logger.info(
            f"New billing cycle detected for company {company_id}: "
            f"{old_period} → {new_period_start}"
        )

        limits = get_tier_limits(tier)

        # Reset peaks to the included base limits
        new_peaks = {
            "documents": limits.get("documents_limit", 0),
            "members": limits.get("members_per_company", 0),
            "strolls": limits.get("strolls_per_month", 0),
            "period_start": new_period_start,
        }

        # Snapshot current usage and re-bill overage for the new cycle
        doc_count = await db.knowledge_sources.count_documents(
            {"company_id": company_id}
        )
        doc_included = limits.get("documents_limit", 0)
        if doc_included != -1 and doc_count > doc_included:
            overage = doc_count - doc_included
            new_peaks["documents"] = doc_count
            for _ in range(overage):
                await enqueue_meter_event(company_id, "document_added")
            logger.info(
                f"Re-billed {overage} document overage for {company_id}"
            )

        members = company.get("members", [])
        member_included = limits.get("members_per_company", 0)
        if member_included != -1 and len(members) > member_included:
            overage = len(members) - member_included
            new_peaks["members"] = len(members)
            for _ in range(overage):
                await enqueue_meter_event(company_id, "member_added")
            logger.info(
                f"Re-billed {overage} member overage for {company_id}"
            )

        await db.companies.update_one(
            {"id": company_id},
            {"$set": {"metered_peaks": new_peaks}},
        )

    # ── Downgrade Auto-Archive ───────────────────────────────────────────

    async def _enforce_downgrade(
        self, company_id: str, new_tier: str
    ) -> None:
        """Auto-archive excess documents and disable excess members when
        a company downgrades to a lower tier.
        """
        from app.core.billing_limits import get_tier_limits, is_unlimited

        limits = get_tier_limits(new_tier)
        now = datetime.now(tz=timezone.utc)

        # ── Archive excess documents ──
        max_docs = limits["documents_limit"]
        if not is_unlimited(max_docs):
            current_docs = await db.knowledge_sources.count_documents(
                {"company_id": company_id, "archived": {"$ne": True}}
            )
            if current_docs > max_docs:
                excess = current_docs - max_docs
                # Archive the most recently uploaded documents first
                to_archive = (
                    db.knowledge_sources.find(
                        {"company_id": company_id, "archived": {"$ne": True}}
                    )
                    .sort("uploaded_at", -1)
                    .limit(excess)
                )
                ids_to_archive = [
                    doc["_id"] async for doc in to_archive
                ]
                if ids_to_archive:
                    await db.knowledge_sources.update_many(
                        {"_id": {"$in": ids_to_archive}},
                        {"$set": {
                            "archived": True,
                            "archived_at": now,
                            "archived_reason": "plan_downgrade",
                        }},
                    )
                    logger.info(
                        f"Archived {len(ids_to_archive)} excess documents "
                        f"for company {company_id} (downgrade to {new_tier})"
                    )

        # ── Disable excess members ──
        max_members = limits["members_per_company"]
        if not is_unlimited(max_members):
            company = await db.companies.find_one({"id": company_id})
            if not company:
                return

            members = company.get("members", [])
            if len(members) > max_members:
                # Keep the oldest members, archive the newest
                # Sort by added_at ascending → keep the first max_members
                sorted_members = sorted(
                    members,
                    key=lambda m: m.get("added_at", now),
                )
                keep = sorted_members[:max_members]
                archived = sorted_members[max_members:]

                await db.companies.update_one(
                    {"id": company_id},
                    {"$set": {
                        "members": keep,
                        "archived_members": archived,
                        "members_archived_at": now,
                        "members_archived_reason": "plan_downgrade",
                    }},
                )
                logger.info(
                    f"Disabled {len(archived)} excess members for "
                    f"company {company_id} (downgrade to {new_tier})"
                )

        # Clear cache so dashboard reflects changes immediately
        from app.core.cache import company_cache
        keys_to_delete = [
            key
            for key in list(company_cache._store.keys())
            if key.startswith(f"company:{company_id}:")
        ]
        for key in keys_to_delete:
            await company_cache.delete(key)


billing_service = BillingService()

