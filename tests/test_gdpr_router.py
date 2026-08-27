def test_gdpr_router_exposes_required_endpoints():
    from app.api.routers.gdpr import router

    routes = {
        (route.path, method) for route in router.routes for method in getattr(route, "methods", [])
    }
    assert ("/companies/{company_id}/gdpr/export", "POST") in routes
    assert ("/companies/{company_id}/gdpr/export/download", "GET") in routes
    assert ("/companies/{company_id}/gdpr/delete", "POST") in routes
    assert (
        "/companies/{company_id}/gdpr/delete/{request_id}/execute",
        "POST",
    ) in routes
    assert ("/companies/{company_id}/gdpr/requests", "GET") in routes
