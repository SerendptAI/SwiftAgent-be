import re
import uuid
from datetime import UTC, datetime

from pymongo import ReturnDocument

from app.core.database import db

_STOPWORDS = {
    "a",
    "an",
    "and",
    "can",
    "could",
    "do",
    "for",
    "how",
    "i",
    "is",
    "me",
    "my",
    "please",
    "the",
    "to",
    "what",
    "where",
    "would",
    "you",
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def normalize_topic(query: str) -> str:
    words = re.findall(r"[a-z0-9]+", query.lower())
    significant = sorted({word for word in words if word not in _STOPWORDS})
    return " ".join(significant) or "general"


def calculate_priority(
    question_count: int,
    unique_sessions: int,
    escalation_count: int,
    average_confidence: float,
) -> tuple[float, str]:
    score = (
        min(question_count, 30) * 1.5
        + min(unique_sessions, 20) * 2.0
        + min(escalation_count, 10) * 4.0
        + max(0.0, 1.0 - average_confidence) * 20.0
    )
    if score >= 55:
        priority = "high"
    elif score >= 25:
        priority = "medium"
    else:
        priority = "low"
    return round(score, 2), priority


async def record_gap_event(
    *,
    company_id: str,
    query: str,
    confidence: float,
    threshold: float,
    session_id: str | None = None,
    escalated: bool = False,
    language: str = "en",
) -> dict:
    """Persist one retrieval miss and aggregate it into a stable topic gap."""
    now = _utcnow()
    topic_key = normalize_topic(query)
    event = {
        "id": str(uuid.uuid4()),
        "company_id": company_id,
        "session_id": session_id,
        "query": query,
        "topic_key": topic_key,
        "retrieval_confidence": max(0.0, min(float(confidence), 1.0)),
        "threshold": threshold,
        "escalated": escalated,
        "language": language,
        "detected_at": now,
    }
    await db.knowledge_gap_events.insert_one(dict(event))

    existing = await db.knowledge_gaps.find_one(
        {"company_id": company_id, "topic_key": topic_key, "status": "open"}
    )
    if existing:
        count = int(existing.get("question_count", 0)) + 1
        old_average = float(existing.get("average_confidence", 0.0))
        average = ((old_average * (count - 1)) + event["retrieval_confidence"]) / count
        sessions = set(existing.get("session_ids", []))
        if session_id:
            sessions.add(session_id)
        escalations = int(existing.get("escalation_count", 0)) + int(escalated)
        queries = list(existing.get("representative_queries", []))
        if query not in queries:
            queries = (queries + [query])[-5:]
        gap_id = existing["id"]
        first_seen = existing.get("first_seen_at", now)
    else:
        count = 1
        average = event["retrieval_confidence"]
        sessions = {session_id} if session_id else set()
        escalations = int(escalated)
        queries = [query]
        gap_id = str(uuid.uuid4())
        first_seen = now

    score, priority = calculate_priority(count, len(sessions), escalations, average)
    gap = {
        "id": gap_id,
        "company_id": company_id,
        "topic_key": topic_key,
        "topic": topic_key,
        "representative_queries": queries,
        "question_count": count,
        "session_ids": sorted(sessions),
        "unique_sessions": len(sessions),
        "escalation_count": escalations,
        "average_confidence": round(average, 4),
        "priority_score": score,
        "priority": priority,
        "status": "open",
        "first_seen_at": first_seen,
        "last_seen_at": now,
        "resolved_at": None,
        "verification": None,
    }
    await db.knowledge_gaps.update_one(
        {"id": gap_id, "company_id": company_id}, {"$set": gap}, upsert=True
    )
    return gap


async def verify_gap(company_id: str, gap_id: str, threshold: float = 0.7) -> dict:
    from app.services.knowledge_service import search_knowledge

    gap = await db.knowledge_gaps.find_one({"id": gap_id, "company_id": company_id})
    if not gap:
        raise ValueError("knowledge gap not found")
    company = await db.companies.find_one({"id": company_id})
    if not company:
        raise ValueError("company not found")

    checks = []
    for query in gap.get("representative_queries", [])[:5]:
        result = await search_knowledge(
            company["user_id"], query, threshold=threshold, company_id=company_id, record_gap=False
        )
        checks.append({"query": query, "confidence": result.get("confidence", 0.0)})
    passed = bool(checks) and all(check["confidence"] >= threshold for check in checks)
    now = _utcnow()
    update = {
        "verification": {
            "passed": passed,
            "threshold": threshold,
            "checks": checks,
            "verified_at": now,
        },
        "last_seen_at": now,
    }
    if passed:
        update.update({"status": "resolved", "resolved_at": now})
    result = await db.knowledge_gaps.find_one_and_update(
        {"id": gap_id, "company_id": company_id},
        {"$set": update},
        return_document=ReturnDocument.AFTER,
    )
    return result
