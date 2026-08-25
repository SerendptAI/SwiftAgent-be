import pytest
from pydantic import ValidationError

from app.models.webhook_models import WebhookEndpointCreate, WebhookEndpointUpdate


def test_endpoint_create_accepts_valid_events():
    payload = WebhookEndpointCreate(
        url="https://hooks.example.com/x",
        events=["ticket.created", "crawl.completed"],
    )
    assert len(payload.events) == 2


def test_endpoint_create_rejects_unknown_events():
    with pytest.raises(ValidationError):
        WebhookEndpointCreate(
            url="https://hooks.example.com/x",
            events=["not.a.real.event"],
        )


def test_endpoint_create_rejects_duplicate_events():
    with pytest.raises(ValidationError):
        WebhookEndpointCreate(
            url="https://hooks.example.com/x",
            events=["ticket.created", "ticket.created"],
        )


def test_endpoint_update_rejects_unknown_events():
    with pytest.raises(ValidationError):
        WebhookEndpointUpdate(events=["nope"])


def test_endpoint_update_allows_partial_changes():
    payload = WebhookEndpointUpdate(enabled=False)
    changes = payload.model_dump(exclude_none=True)
    assert changes == {"enabled": False}


def test_webhook_router_exposes_required_endpoints():
    from app.api.routers.webhooks import router

    routes = {
        (route.path, method) for route in router.routes for method in getattr(route, "methods", [])
    }
    assert ("/companies/{company_id}/webhooks", "GET") in routes
    assert ("/companies/{company_id}/webhooks", "POST") in routes
    assert ("/companies/{company_id}/webhooks/{endpoint_id}", "PATCH") in routes
    assert ("/companies/{company_id}/webhooks/{endpoint_id}", "DELETE") in routes
    assert (
        "/companies/{company_id}/webhooks/{endpoint_id}/rotate-secret",
        "POST",
    ) in routes
    assert ("/companies/{company_id}/webhooks/deliveries", "GET") in routes
