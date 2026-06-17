"""
Pydantic models for Company API Integrations.

Companies register their internal APIs — base URL, endpoints, API key, and
free-text documentation. The agent uses these as read-only (GET) verification
tools at runtime.
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, field_validator


class APIEndpoint(BaseModel):
    """A single API endpoint the agent can call."""

    name: str  # e.g. "verify_order"
    path: str  # e.g. "/api/v1/orders/{order_id}"
    description: str  # natural-language description for the agent
    query_params: Optional[dict] = None  # optional default query params
    headers: Optional[dict] = None  # optional extra headers per-endpoint


class IntegrationCreate(BaseModel):
    """Payload to register a new company API integration."""

    name: str  # e.g. "Internal Order API"
    base_url: str  # e.g. "https://api.acme.com"
    api_key: Optional[str] = None  # raw key — encrypted before storage
    auth_header: str = "Authorization"  # header name for the key
    auth_prefix: str = "Bearer"  # prefix, e.g. "Bearer", "Api-Key", ""
    documentation_url: Optional[str] = None  # URL to extract documentation from
    documentation: str = ""  # free-text API docs (markdown/plain)
    endpoints: List[APIEndpoint] = []  # registered GET endpoints

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        return v.rstrip("/")


class IntegrationUpdate(BaseModel):
    """Partial update — api_key is optional (re-encrypt if provided)."""

    name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None  # if provided, re-encrypts
    auth_header: Optional[str] = None
    auth_prefix: Optional[str] = None
    documentation_url: Optional[str] = None
    documentation: Optional[str] = None
    endpoints: Optional[List[APIEndpoint]] = None

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not v.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        return v.rstrip("/") if v else v


class IntegrationResponse(BaseModel):
    """Returned to the dashboard — never exposes the raw or encrypted key."""

    id: str
    company_id: str
    name: str
    base_url: str
    auth_header: str
    auth_prefix: str
    documentation_url: Optional[str] = None
    documentation: str
    endpoints: List[APIEndpoint]
    active: bool
    created_at: datetime
    updated_at: Optional[datetime] = None


class IntegrationSummary(BaseModel):
    """Lightweight summary for agent tool discovery."""

    name: str
    endpoints: List[APIEndpoint]
