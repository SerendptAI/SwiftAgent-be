def test_audit_log_router_exposes_required_endpoints():
    from app.api.routers.audit_log import router

    routes = {
        (route.path, method)
        for route in router.routes
        for method in getattr(route, "methods", [])
    }
    assert ("/companies/{company_id}/audit-log", "GET") in routes
    assert ("/companies/{company_id}/audit-log/{event_id}", "GET") in routes


def test_audit_middleware_audits_state_changing_methods_only():
    from app.core.audit_middleware import AUDITED_METHODS

    assert AUDITED_METHODS == {"POST", "PUT", "PATCH", "DELETE"}


def test_company_id_extracted_from_path():
    from app.core.audit_middleware import _company_id_from_path

    path = "/api/v1/knowledge-crawl/companies/comp_123/run"
    assert _company_id_from_path(path) == "comp_123"
    assert _company_id_from_path("/api/v1/users/me") is None
