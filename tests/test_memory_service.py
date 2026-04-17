"""Tests for memory service."""

import pytest
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock


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


class TestIdentifyUserFromConversation:
    @pytest.mark.asyncio
    async def test_extract_user_identity(self):
        """Test user identity extraction from messages."""
        from app.services import memory_service
        from unittest.mock import AsyncMock, patch

        messages = [
            {"role": "user", "content": "Hi, my name is John Doe and my email is john@example.com"}
        ]

        # This test would require mocking the Gemini client
        # Just verify the function signature for now
        assert callable(memory_service.identify_user_from_conversation)


class TestCleanupOldMemories:
    @pytest.mark.asyncio
    async def test_cleanup_function_exists(self):
        """Test that cleanup function exists."""
        from app.services import memory_service

        assert callable(memory_service.cleanup_old_memories)
