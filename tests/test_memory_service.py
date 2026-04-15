"""Tests for memory service."""

import pytest
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock


class TestMemoryServiceWorkingMemory:
    def setup_method(self):
        from app.services import memory_service

        memory_service._working_memory_store.clear()

    def test_save_and_get_working_memory(self):
        from app.models.memory_models import WorkingMemory
        from app.services import memory_service

        wm = WorkingMemory(
            session_id="sess_123",
            identified_user=True,
            user_name="John",
        )

        asyncio.run(memory_service.save_working_memory(wm))
        retrieved = asyncio.run(memory_service.get_working_memory("sess_123"))

        assert retrieved is not None
        assert retrieved.session_id == "sess_123"
        assert retrieved.user_name == "John"
        assert retrieved.identified_user is True

    def test_delete_working_memory(self):
        from app.models.memory_models import WorkingMemory
        from app.services import memory_service

        wm = WorkingMemory(session_id="sess_123")
        asyncio.run(memory_service.save_working_memory(wm))
        asyncio.run(memory_service.delete_working_memory("sess_123"))

        result = asyncio.run(memory_service.get_working_memory("sess_123"))
        assert result is None


class TestFormatMemoryContext:
    def test_format_empty_context(self):
        from app.models.memory_models import MemoryContext
        from app.services import memory_service

        ctx = MemoryContext()
        formatted = memory_service.format_memory_context(ctx)
        assert formatted == ""

    def test_format_context_with_working_memory(self):
        from app.models.memory_models import MemoryContext, WorkingMemory
        from app.services import memory_service

        wm = WorkingMemory(
            session_id="sess_123",
            identified_user=True,
            user_name="John",
            current_issue="Transaction issue",
            issue_resolved=True,
        )
        ctx = MemoryContext(working_memory=wm)
        formatted = memory_service.format_memory_context(ctx)

        assert "Current Session:" in formatted
        assert "John" in formatted
        assert "Transaction issue" in formatted

    def test_format_context_with_semantic_memories(self):
        from app.models.memory_models import MemoryContext, SemanticMemory
        from app.services import memory_service

        semantic = SemanticMemory(
            user_id="user_123",
            company_id="comp_456",
            content="User prefers Spanish language",
        )
        ctx = MemoryContext(semantic_memories=[semantic])
        formatted = memory_service.format_memory_context(ctx)

        assert "Learned about user:" in formatted
        assert "Spanish language" in formatted

    def test_format_context_with_episodic_memories(self):
        from app.models.memory_models import MemoryContext, EpisodeSummary
        from app.services import memory_service

        episode = EpisodeSummary(
            session_id="sess_123",
            company_id="comp_456",
            summary="User asked about ETH transaction",
            outcome="resolved",
        )
        ctx = MemoryContext(episodic_memories=[episode])
        formatted = memory_service.format_memory_context(ctx)

        assert "Previous conversations:" in formatted
        assert "ETH transaction" in formatted

    def test_format_context_with_multiple_layers(self):
        from app.models.memory_models import (
            MemoryContext,
            WorkingMemory,
            SemanticMemory,
            EpisodeSummary,
        )
        from app.services import memory_service

        wm = WorkingMemory(
            session_id="sess_123",
            identified_user=True,
            user_name="Jane",
        )
        semantic = SemanticMemory(
            user_id="user_123",
            company_id="comp_456",
            content="Prefers detailed explanations",
        )
        episode = EpisodeSummary(
            session_id="sess_old",
            company_id="comp_456",
            summary="Previous issue resolved",
            outcome="resolved",
        )
        ctx = MemoryContext(
            working_memory=wm,
            semantic_memories=[semantic],
            episodic_memories=[episode],
        )
        formatted = memory_service.format_memory_context(ctx)

        assert "Current Session:" in formatted
        assert "Learned about user:" in formatted
        assert "Previous conversations:" in formatted
        assert "Jane" in formatted


class TestLoadMemoryContext:
    @pytest.mark.asyncio
    async def test_load_memory_context_empty(self):
        from app.services import memory_service
        import app.services.memory_service as ms

        ms._working_memory_store.clear()

        with patch("app.services.memory_service.db") as mock_db:
            mock_db.episodic_episodes.find.return_value.sort.return_value.limit.return_value.to_list = AsyncMock(
                return_value=[]
            )
            mock_db.qdrant_client.query_points = AsyncMock(return_value=MagicMock(points=[]))

            ctx = await memory_service.load_memory_context(
                company_id="comp_123",
                user_id=None,
                session_id=None,
            )

            assert ctx.episodic_memories == []
            assert ctx.semantic_memories == []
            assert ctx.working_memory is None
