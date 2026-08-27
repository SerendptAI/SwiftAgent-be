from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_normalize_topic_groups_equivalent_phrasing():
    from app.services.knowledge_gap_service import normalize_topic

    assert normalize_topic("Can I pause my subscription?") == normalize_topic(
        "How can I pause the subscription"
    )


def test_calculate_priority_rewards_frequency_and_escalations():
    from app.services.knowledge_gap_service import calculate_priority

    low_score, low_label = calculate_priority(2, 2, 0, 0.55)
    high_score, high_label = calculate_priority(20, 12, 7, 0.2)

    assert high_score > low_score
    assert low_label == "low"
    assert high_label == "high"


@pytest.mark.asyncio
async def test_record_gap_event_upserts_aggregated_gap():
    from app.services import knowledge_gap_service as service

    db = MagicMock()
    db.knowledge_gaps.find_one = AsyncMock(return_value=None)
    db.knowledge_gaps.update_one = AsyncMock()
    db.knowledge_gap_events.insert_one = AsyncMock()

    with patch.object(service, "db", db):
        gap = await service.record_gap_event(
            company_id="comp",
            query="Can I pause my subscription?",
            confidence=0.25,
            threshold=0.7,
            session_id="session-1",
            escalated=True,
        )

    assert gap["question_count"] == 1
    assert gap["escalation_count"] == 1
    db.knowledge_gap_events.insert_one.assert_awaited_once()
    db.knowledge_gaps.update_one.assert_awaited_once()
    assert db.knowledge_gaps.update_one.await_args.kwargs["upsert"] is True


@pytest.mark.asyncio
async def test_verify_gap_resolves_only_when_all_representative_queries_pass():
    from app.services import knowledge_gap_service as service

    db = MagicMock()
    db.knowledge_gaps.find_one = AsyncMock(
        return_value={
            "id": "gap-1",
            "company_id": "comp",
            "representative_queries": ["pause subscription", "freeze account"],
        }
    )
    db.companies.find_one = AsyncMock(return_value={"id": "comp", "user_id": "owner"})
    db.knowledge_gaps.find_one_and_update = AsyncMock(
        return_value={"id": "gap-1", "status": "resolved"}
    )
    search = AsyncMock(
        side_effect=[
            {"confidence": 0.82, "results": [{}], "escalate": False},
            {"confidence": 0.78, "results": [{}], "escalate": False},
        ]
    )

    with (
        patch.object(service, "db", db),
        patch("app.services.knowledge_service.search_knowledge", search),
    ):
        result = await service.verify_gap("comp", "gap-1", threshold=0.7)

    assert result["status"] == "resolved"
    update = db.knowledge_gaps.find_one_and_update.await_args.args[1]["$set"]
    assert update["status"] == "resolved"
    assert update["verification"]["passed"] is True
