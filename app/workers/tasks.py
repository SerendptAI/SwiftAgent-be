"""Worker task functions executed by ARQ worker processes.

Each function is a thin async wrapper around a service call. They run in a
separate process (``arq app.core.queue.WorkerSettings``) with their own DB
connections — never import web-only singletons like the Playwright browser.
"""

import logging

logger = logging.getLogger(__name__)


async def knowledge_crawl(ctx, company_id: str, trigger: str = "scheduled"):
    """Run one company knowledge-base crawl inside the worker."""
    from app.services.knowledge_crawl_service import run_company_crawl

    return await run_company_crawl(company_id, trigger=trigger)


async def gdpr_export(ctx, company_id: str, requested_by: str):
    """Build and persist a GDPR export bundle for one company."""
    from app.services.gdpr_service import export_company_data

    return await export_company_data(company_id)


async def gdpr_delete(ctx, company_id: str, request_id: str):
    """Execute a confirmed GDPR deletion request."""
    from app.services.gdpr_service import run_deletion

    return await run_deletion(request_id, company_id)


async def retention_sweep(ctx, company_id: str | None = None):
    """Apply retention windows (delete/anonymize) for one or all companies."""
    from app.services.conversation_privacy_service import apply_retention_sweep

    return await apply_retention_sweep(company_id)


all_worker_tasks = [
    knowledge_crawl,
    gdpr_export,
    gdpr_delete,
    retention_sweep,
]
