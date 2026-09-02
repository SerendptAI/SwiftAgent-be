#!/usr/bin/env python
"""Entrypoint for ARQ worker processes.

Usage:
    python -m app.workers.worker

Requires REDIS_URL to be set. The worker executes durable jobs (knowledge
crawls, GDPR exports/deletions, retention sweeps) independently of web
replicas so restarts never kill in-flight work.
"""

import logging

from arq import run_worker

from app.core.queue import worker_functions


class RuntimeWorkerSettings:
    """WorkerSettings consumed by arq.run_worker / the `arq` CLI."""

    functions = worker_functions()
    max_jobs = 4
    job_timeout = 3600
    max_tries = 3
    health_check_interval = 30
    burst = False

    @classmethod
    async def startup(cls, ctx):
        from app.core.database import create_indexes

        try:
            await create_indexes()
        except Exception:
            logging.getLogger(__name__).exception("Worker startup index creation failed")

    @classmethod
    async def shutdown(cls, ctx):
        pass


if __name__ == "__main__":
    run_worker(RuntimeWorkerSettings)
