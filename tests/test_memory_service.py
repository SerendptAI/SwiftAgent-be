"""Tests for memory service - session-scoped only."""

import pytest


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


class TestCleanupOldMemories:
    @pytest.mark.asyncio
    async def test_cleanup_function_exists(self):
        """Test that cleanup function exists."""
        from app.services import memory_service

        assert callable(memory_service.cleanup_old_memories)
