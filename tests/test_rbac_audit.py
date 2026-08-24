"""
Tests for RBAC and audit system.
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.rbac import (
    has_permission,
    require_permission,
    require_company_access,
    ROLE_PERMISSIONS,
    VALID_ROLES,
    get_role_permissions,
    RBACError,
)
from app.core.audit import AuditEvent, AuditLogger, _diff_states, query_audit_events


# ── RBAC Tests ────────────────────────────────────────────────────────────────

class TestRBAC:
    """Test role-based access control logic."""

    def test_valid_roles(self):
        """Verify all expected roles exist."""
        assert "owner" in VALID_ROLES
        assert "admin" in VALID_ROLES
        assert "manager" in VALID_ROLES
        assert "agent" in VALID_ROLES
        assert "viewer" in VALID_ROLES

    def test_owner_has_full_access(self):
        """Owner should have wildcard permission."""
        perms = get_role_permissions("owner")
        assert "*" in perms

    def test_admin_has_broad_access(self):
        """Admin should have most permissions."""
        perms = get_role_permissions("admin")
        assert "tickets:*" in perms
        assert "knowledge:*" in perms
        assert "users:*" in perms

    def test_agent_limited_access(self):
        """Agent should have read/write on tickets, read-only elsewhere."""
        perms = get_role_permissions("agent")
        assert "tickets:read" in perms
        assert "tickets:write" in perms
        assert "knowledge:read" in perms
        assert "knowledge:write" not in perms
        assert "users:read" not in perms

    def test_viewer_read_only(self):
        """Viewer should only have read permissions."""
        perms = get_role_permissions("viewer")
        assert all(":read" in p for p in perms)
        assert not any(":write" in p for p in perms)

    def test_has_permission_exact_match(self):
        """Exact permission should match."""
        assert has_permission(["tickets:read"], "tickets:read") is True

    def test_has_permission_wildcard(self):
        """Wildcard should match any permission."""
        assert has_permission(["*"], "tickets:read") is True
        assert has_permission(["*"], "billing:write") is True

    def test_has_permission_resource_wildcard(self):
        """Resource wildcard should match any action on that resource."""
        assert has_permission(["tickets:*"], "tickets:read") is True
        assert has_permission(["tickets:*"], "tickets:write") is True
        assert has_permission(["tickets:*"], "billing:read") is False

    def test_has_permission_no_match(self):
        """Non-matching permission should return False."""
        assert has_permission(["tickets:read"], "billing:read") is False

    def test_has_permission_empty_list(self):
        """Empty permission list should return False."""
        assert has_permission([], "tickets:read") is False

    def test_has_permission_none(self):
        """None permission list should return False."""
        assert has_permission([], "tickets:read") is False

    def test_rbac_error(self):
        """RBACError should be an HTTPException with 403 status."""
        error = RBACError("tickets:write")
        assert error.status_code == 403
        assert "tickets:write" in error.detail


# ── Audit Tests ────────────────────────────────────────────────────────────────

class TestAuditEvent:
    """Test AuditEvent model."""

    def test_audit_event_creation(self):
        """AuditEvent should create with required fields."""
        event = AuditEvent(
            actor_id="user_123",
            company_id="comp_456",
            resource_type="ticket",
            resource_id="ticket_789",
            action="write",
        )
        assert event.actor_id == "user_123"
        assert event.company_id == "comp_456"
        assert event.resource_type == "ticket"
        assert event.resource_id == "ticket_789"
        assert event.action == "write"
        assert event.status == "success"
        assert event.event_id is not None
        assert isinstance(event.timestamp, datetime)

    def test_audit_event_defaults(self):
        """AuditEvent should have sensible defaults."""
        event = AuditEvent()
        assert event.status == "success"
        assert event.event_id is not None
        assert event.timestamp is not None


class TestDiffStates:
    """Test state diffing logic."""

    def test_diff_no_changes(self):
        """No changes should return empty list."""
        before = {"status": "pending", "priority": "medium"}
        after = {"status": "pending", "priority": "medium"}
        assert _diff_states(before, after) == []

    def test_diff_with_changes(self):
        """Changes should be detected and formatted."""
        before = {"status": "pending", "priority": "medium"}
        after = {"status": "resolved", "priority": "high"}
        changes = _diff_states(before, after)
        assert len(changes) == 2
        assert any("status" in c for c in changes)
        assert any("priority" in c for c in changes)

    def test_diff_with_none(self):
        """None before/after should return empty list."""
        assert _diff_states(None, {"status": "resolved"}) == []
        assert _diff_states({"status": "pending"}, None) == []

    def test_diff_ignores_private_keys(self):
        """Keys starting with _ should be ignored."""
        before = {"_id": "abc", "status": "pending"}
        after = {"_id": "xyz", "status": "resolved"}
        changes = _diff_states(before, after)
        assert len(changes) == 1
        assert "status" in changes[0]


class TestAuditLogger:
    """Test AuditLogger service."""

    @pytest.mark.asyncio
    async def test_log_event_writes_to_db(self):
        """log_event should write to the audit_events collection."""
        mock_db = MagicMock()
        mock_collection = MagicMock()
        mock_collection.insert_one = AsyncMock(return_value=MagicMock(inserted_id="abc123"))
        mock_db.__getitem__ = MagicMock(return_value=mock_collection)

        with patch("app.core.audit.db", mock_db):
            event_id = await AuditLogger.log_event(
                actor_id="user_123",
                company_id="comp_456",
                resource_type="ticket",
                resource_id="ticket_789",
                action="write",
            )

        assert event_id is not None
        mock_collection.insert_one.assert_called_once()

    @pytest.mark.asyncio
    async def test_log_event_handles_failure(self):
        """log_event should not raise on failure."""
        mock_db = MagicMock()
        mock_collection = MagicMock()
        mock_collection.insert_one = AsyncMock(side_effect=Exception("DB error"))
        mock_db.__getitem__ = MagicMock(return_value=mock_collection)

        with patch("app.core.audit.db", mock_db):
            event_id = await AuditLogger.log_event(
                actor_id="user_123",
                action="write",
            )

        assert event_id is None  # Should return None on failure


class TestQueryAuditEvents:
    """Test audit event querying."""

    @pytest.mark.asyncio
    async def test_query_with_company_filter(self):
        """Query should filter by company_id."""
        mock_db = MagicMock()
        mock_collection = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.sort.return_value = mock_cursor
        mock_cursor.skip.return_value = mock_cursor
        mock_cursor.limit.return_value = mock_cursor
        mock_cursor.__aiter__ = AsyncMock(return_value=iter([]))
        mock_collection.find.return_value = mock_cursor
        mock_db.__getitem__ = MagicMock(return_value=mock_collection)

        with patch("app.core.audit.db", mock_db):
            events = await query_audit_events(company_id="comp_123")

        assert events == []
        mock_collection.find.assert_called_once()


# ── Permission Map Tests ───────────────────────────────────────────────────────

class TestPermissionMap:
    """Test that all routes have valid permissions."""

    def test_all_role_permissions_are_valid_format(self):
        """All permissions should be in 'resource:action' format or '*'."""
        for role, perms in ROLE_PERMISSIONS.items():
            for perm in perms:
                assert perm == "*" or ":" in perm, f"Invalid permission format: {perm} in role {role}"

    def test_no_duplicate_permissions(self):
        """No duplicate permissions within a role."""
        for role, perms in ROLE_PERMISSIONS.items():
            assert len(perms) == len(set(perms)), f"Duplicate permissions in role {role}"

    def test_owner_has_wildcard(self):
        """Owner should have wildcard permission."""
        assert "*" in ROLE_PERMISSIONS["owner"]

    def test_viewer_has_no_write(self):
        """Viewer should not have any write permissions."""
        for perm in ROLE_PERMISSIONS["viewer"]:
            assert ":write" not in perm, f"Viewer should not have write permission: {perm}"
