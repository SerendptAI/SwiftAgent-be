def test_knowledge_crawl_router_exposes_required_endpoints():
    from app.api.routers.knowledge_crawl import router

    routes = {(route.path, method) for route in router.routes for method in route.methods}
    expected = {
        ("/companies/{company_id}/config", "PUT"),
        ("/companies/{company_id}/config", "GET"),
        ("/companies/{company_id}/config", "DELETE"),
        ("/companies/{company_id}/run", "POST"),
        ("/companies/{company_id}/runs", "GET"),
        ("/runs/{run_id}", "GET"),
        ("/companies/{company_id}/freshness", "GET"),
        ("/companies/{company_id}/pages", "GET"),
        ("/companies/{company_id}/gaps", "GET"),
        ("/companies/{company_id}/gaps/{gap_id}", "PATCH"),
        ("/companies/{company_id}/gaps/{gap_id}/verify", "POST"),
    }
    assert expected <= routes
