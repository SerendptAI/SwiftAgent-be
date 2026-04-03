"""Tests for the security module (JWT token creation/validation)."""

import pytest
from datetime import timedelta
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_access_token,
    decode_refresh_token,
)


class TestSecurity:
    def test_create_and_decode_access_token(self):
        token = create_access_token(data={"sub": "user123"})
        payload = decode_access_token(token)
        assert payload is not None
        assert payload["sub"] == "user123"
        assert "jti" in payload

    def test_create_and_decode_refresh_token(self):
        token = create_refresh_token(data={"sub": "user123"})
        payload = decode_refresh_token(token)
        assert payload is not None
        assert payload["sub"] == "user123"
        assert payload["type"] == "refresh"
        assert "jti" in payload

    def test_decode_invalid_access_token(self):
        result = decode_access_token("invalid.token.here")
        assert result is None

    def test_decode_invalid_refresh_token(self):
        result = decode_refresh_token("invalid.token.here")
        assert result is None

    def test_access_token_rejects_wrong_type(self):
        access = create_access_token(data={"sub": "user123"})
        result = decode_refresh_token(access)
        assert result is None

    def test_custom_expiry(self):
        token = create_access_token(data={"sub": "user123"}, expires_delta=timedelta(minutes=5))
        payload = decode_access_token(token)
        assert payload is not None
        assert payload["sub"] == "user123"
