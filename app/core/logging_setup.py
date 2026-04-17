"""Central logging helpers: optional fields used in format strings must exist on every LogRecord."""

from __future__ import annotations

import logging
import contextvars

_installed = False

request_id_context_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)


def install_default_log_record_fields() -> None:
    """Default missing format fields (e.g. %(request_id)s) so startup and library logs do not crash formatters."""
    global _installed
    if _installed:
        return
    _installed = True

    old_factory = logging.getLogRecordFactory()

    def record_factory(*args, **kwargs):
        record = old_factory(*args, **kwargs)
        if not hasattr(record, "request_id"):
            record.request_id = request_id_context_var.get()
        return record

    logging.setLogRecordFactory(record_factory)
