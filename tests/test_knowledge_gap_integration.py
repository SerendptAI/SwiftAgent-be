from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_low_confidence_search_records_knowledge_gap():
    from app.services import knowledge_service

    with (
        patch.object(
            knowledge_service.qdrant_client, "collection_exists", AsyncMock(return_value=False)
        ),
        patch("app.services.knowledge_gap_service.record_gap_event", AsyncMock()) as record,
    ):
        result = await knowledge_service.search_knowledge(
            "owner",
            "Can I pause my subscription?",
            company_id="comp",
            session_id="session-1",
        )

    assert result["escalate"] is True
    record.assert_awaited_once()
    assert record.await_args.kwargs["company_id"] == "comp"
    assert record.await_args.kwargs["session_id"] == "session-1"
