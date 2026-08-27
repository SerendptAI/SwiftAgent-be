"""Conversation retention windows and PII redaction.

Two complementary controls, both configurable per company:

1. **PII redaction on ingestion** — customer-visible message text is scrubbed
   of emails, phone numbers, and card-pattern numbers before it is persisted.
   The LLM still sees the original text at request time; only stored history
   is redacted.

2. **Retention windows** — a background sweep deletes or anonymizes
   conversations older than the company's configured window. Anonymize keeps
   the conversation for analytics but strips message content; delete removes
   the documents entirely.
"""

import logging
import re
from datetime import UTC, datetime, timedelta

from app.core.database import db

logger = logging.getLogger(__name__)

REDACTED = "[REDACTED]"

# Email: conservative pattern avoiding trailing punctuation in matches.
_EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Phone: international/US shapes — +country, (555) 123-4567, 555-123-4567,
# 555.123.4567, 10+ digit runs. Requires at least 10 digits total.
_PHONE_PATTERN = re.compile(
    r"(?<![\w])"
    r"(?:\+?\d{1,3}[\s.\-]?)?"
    r"(?:\(\d{2,4}\)[\s.\-]?)?\d{3}[\s.\-]?\d{3,4}[\s.\-]?\d{0,4}"
    r"(?![\w])"
)

# Card numbers: 13-19 digits with optional separators; validated with Luhn.
_CARD_PATTERN = re.compile(r"(?<![\w])\d(?:[\s\-]?\d){12,18}(?![\w])")


def _luhn_valid(candidate: str) -> bool:
    digits = [int(ch) for ch in candidate if ch.isdigit()]
    if len(digits) < 13:
        return False
    checksum = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def _looks_like_phone(candidate: str) -> bool:
    digits = re.sub(r"\D", "", candidate)
    return 10 <= len(digits) <= 15


def redact_pii(text: str) -> str:
    """Scrub emails, phone-like numbers, and card-like numbers from text."""
    if not text:
        return text

    def _card_replacer(match: re.Match) -> str:
        return REDACTED if _luhn_valid(match.group()) else match.group()

    def _phone_replacer(match: re.Match) -> str:
        return REDACTED if _looks_like_phone(match.group()) else match.group()

    redacted = _CARD_PATTERN.sub(_card_replacer, text)
    redacted = _EMAIL_PATTERN.sub(REDACTED, redacted)
    redacted = _PHONE_PATTERN.sub(_phone_replacer, redacted)
    return redacted


async def get_retention_config(company_id: str) -> dict:
    """Effective retention settings for one company.

    Companies override via their document (``retention_policy``); defaults
    keep everything for 365 days and never anonymize.
    """
    company = await db.companies.find_one({"id": company_id}, {"retention_policy": 1})
    policy = (company or {}).get("retention_policy") or {}
    days = policy.get("retention_days")
    if not isinstance(days, int) or days < 1:
        days = 365
    mode = policy.get("expired_action")
    if mode not in {"delete", "anonymize", "retain"}:
        mode = "retain" if not policy else "delete"
    if not policy:
        mode = "retain"
    redact = policy.get("redact_pii", True)
    return {
        "retention_days": days,
        "expired_action": mode,
        "redact_pii": bool(redact),
    }


async def redact_message_on_ingest(company_id: str, content: str) -> str:
    """Apply the company's ingestion redaction policy to message text."""
    config = await get_retention_config(company_id)
    if not config["redact_pii"]:
        return content
    return redact_pii(content)


async def apply_retention_sweep(company_id: str | None = None) -> dict:
    """Delete or anonymize conversations past their retention window.

    Runs per company using that company's own policy. With no ``company_id``
    it sweeps every company that has a retention_policy configured.
    """
    query: dict = {}
    if company_id:
        query["id"] = company_id
    else:
        query["retention_policy"] = {"$exists": True, "$ne": {}}

    companies = await db.companies.find(query).to_list(length=None)
    totals = {"companies": 0, "conversations_deleted": 0, "conversations_anonymized": 0}

    for company in companies:
        cid = company["id"]
        config = await get_retention_config(cid)
        if config["expired_action"] == "retain":
            continue

        cutoff = datetime.now(UTC) - timedelta(days=config["retention_days"])

        target_query = {
            "company_id": cid,
            "updated_at": {"$lt": cutoff},
        }

        if config["expired_action"] == "delete":
            result = await db.widget_conversations.delete_many(target_query)
            totals["conversations_deleted"] += result.deleted_count
        else:
            result = await db.widget_conversations.update_many(
                target_query,
                {
                    "$set": {
                        "messages": [],
                        "subject": "[removed]",
                        "sdk_user_email": None,
                        "visitor_ip": None,
                        "anonymized_at": cutoff,
                    }
                },
            )
            totals["conversations_anonymized"] += result.modified_count
        totals["companies"] += 1

    return totals


async def ensure_retention_indexes() -> None:
    await db.widget_conversations.create_index([("company_id", 1), ("updated_at", -1)])
