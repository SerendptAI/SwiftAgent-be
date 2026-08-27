"""Webhook endpoint and delivery models."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, HttpUrl, field_validator


class WebhookEventType(StrEnum):
    TICKET_CREATED = "ticket.created"
    TICKET_ESCALATED = "ticket.escalated"
    CRAWL_COMPLETED = "crawl.completed"
    CRAWL_FAILED = "crawl.failed"
    KNOWLEDGE_UPDATED = "knowledge.updated"
    GAP_DETECTED = "gap.detected"
    HANDOFF_INITIATED = "handoff.initiated"


AVAILABLE_EVENTS = [event.value for event in WebhookEventType]


class WebhookEndpointCreate(BaseModel):
    url: HttpUrl
    events: list[str] = Field(min_length=1)
    description: str | None = None

    @field_validator("events")
    @classmethod
    def validate_events(cls, value: list[str]) -> list[str]:
        unknown = [event for event in value if event not in AVAILABLE_EVENTS]
        if unknown:
            raise ValueError(f"unknown event types: {', '.join(sorted(unknown))}")
        if len(value) != len(set(value)):
            raise ValueError("duplicate event types are not allowed")
        return value


class WebhookEndpointUpdate(BaseModel):
    url: HttpUrl | None = None
    events: list[str] | None = None
    description: str | None = None
    enabled: bool | None = None

    @field_validator("events")
    @classmethod
    def validate_events(cls, value: list[str] | None) -> list[str] | None:
        if value is not None:
            unknown = [event for event in value if event not in AVAILABLE_EVENTS]
            if unknown:
                raise ValueError(f"unknown event types: {', '.join(sorted(unknown))}")
            if len(value) != len(set(value)):
                raise ValueError("duplicate event types are not allowed")
        return value


class WebhookEndpoint(BaseModel):
    id: str
    company_id: str
    url: str
    events: list[str]
    description: str | None = None
    secret: str
    enabled: bool = True
    created_at: datetime
    updated_at: datetime


class WebhookDelivery(BaseModel):
    id: str
    endpoint_id: str
    company_id: str
    event_type: str
    status: str
    status_code: int | None = None
    attempt: int
    response_body: str | None = None
    error: str | None = None
    created_at: datetime
