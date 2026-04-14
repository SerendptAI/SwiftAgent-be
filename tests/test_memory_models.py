"""Tests for memory models."""

import pytest
from datetime import datetime
from app.models.memory_models import (
    EpisodeSummary,
    EpisodicEvent,
    SemanticMemory,
    WorkingMemory,
    MemoryContext,
    MemoryType,
)


class TestEpisodeSummary:
    def test_create_episode(self):
        episode = EpisodeSummary(
            session_id="sess_123",
            company_id="comp_456",
            user_id="user_789",
            summary="User asked about transaction status",
            topics=["transactions", "crypto"],
            tools_used=["lookup_transaction"],
            outcome="resolved",
            message_count=5,
        )
        assert episode.session_id == "sess_123"
        assert episode.company_id == "comp_456"
        assert episode.user_id == "user_789"
        assert episode.summary == "User asked about transaction status"
        assert episode.topics == ["transactions", "crypto"]
        assert episode.tools_used == ["lookup_transaction"]
        assert episode.outcome == "resolved"
        assert episode.message_count == 5

    def test_default_values(self):
        episode = EpisodeSummary(
            session_id="sess_123",
            company_id="comp_456",
            summary="default summary",
        )
        assert episode.user_id is None
        assert episode.summary == "default summary"
        assert episode.topics == []
        assert episode.tools_used == []
        assert episode.outcome == "unknown"
        assert episode.message_count == 0


class TestEpisodicEvent:
    def test_create_event(self):
        event = EpisodicEvent(
            session_id="sess_123",
            event_type="tool_call",
            event_data={"tool": "lookup_transaction", "result": "success"},
        )
        assert event.session_id == "sess_123"
        assert event.event_type == "tool_call"
        assert event.event_data["tool"] == "lookup_transaction"

    def test_default_event_data(self):
        event = EpisodicEvent(
            session_id="sess_123",
            event_type="diagnosis",
        )
        assert event.event_data == {}


class TestSemanticMemory:
    def test_create_semantic_memory(self):
        memory = SemanticMemory(
            user_id="user_123",
            company_id="comp_456",
            memory_type="user_profile",
            content="User prefers concise responses",
            importance=0.8,
        )
        assert memory.user_id == "user_123"
        assert memory.company_id == "comp_456"
        assert memory.memory_type == "user_profile"
        assert memory.content == "User prefers concise responses"
        assert memory.importance == 0.8

    def test_default_values(self):
        memory = SemanticMemory(
            user_id="user_123",
            company_id="comp_456",
            content="Test content",
        )
        assert memory.id is None
        assert memory.memory_type == "user_profile"
        assert memory.embedding is None
        assert memory.importance == 0.5


class TestWorkingMemory:
    def test_create_working_memory(self):
        wm = WorkingMemory(
            session_id="sess_123",
            identified_user=True,
            user_name="John Doe",
            user_email="john@test.com",
            current_issue="Transaction stuck",
            issue_resolved=False,
        )
        assert wm.session_id == "sess_123"
        assert wm.identified_user is True
        assert wm.user_name == "John Doe"
        assert wm.user_email == "john@test.com"
        assert wm.current_issue == "Transaction stuck"
        assert wm.issue_resolved is False

    def test_default_values(self):
        wm = WorkingMemory(session_id="sess_123")
        assert wm.identified_user is False
        assert wm.user_name is None
        assert wm.user_email is None
        assert wm.current_issue is None
        assert wm.issue_resolved is False
        assert wm.pending_actions == []
        assert wm.context_window == []


class TestMemoryContext:
    def test_create_empty_context(self):
        ctx = MemoryContext()
        assert ctx.episodic_memories == []
        assert ctx.semantic_memories == []
        assert ctx.working_memory is None
        assert ctx.recent_conversations == []

    def test_create_context_with_data(self):
        episode = EpisodeSummary(
            session_id="sess_123",
            company_id="comp_456",
            summary="Previous conversation",
        )
        semantic = SemanticMemory(
            user_id="user_123",
            company_id="comp_456",
            content="User preference",
        )
        working = WorkingMemory(session_id="sess_789")

        ctx = MemoryContext(
            episodic_memories=[episode],
            semantic_memories=[semantic],
            working_memory=working,
            recent_conversations=["sess_111"],
        )

        assert len(ctx.episodic_memories) == 1
        assert len(ctx.semantic_memories) == 1
        assert ctx.working_memory.session_id == "sess_789"
        assert ctx.recent_conversations == ["sess_111"]


class TestMemoryType:
    def test_enum_values(self):
        assert MemoryType.EPISODIC == "episodic"
        assert MemoryType.SEMANTIC == "semantic"
        assert MemoryType.WORKING == "working"
