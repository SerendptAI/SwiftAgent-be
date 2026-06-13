"""
Langfuse observability integration for SwiftAgent-be.

Provides a singleton Langfuse client and the @observe() decorator used to
trace every LLM call (Anthropic, Gemini, OpenRouter) with:
  - full prompt / completion capture
  - token usage + latency
  - tool call spans as nested child observations
  - company_id / session_id / user_id as trace metadata

Usage
-----
    from app.core.langfuse import observe, langfuse_client

    # Wrap any async function — its args become trace input, its return value becomes output.
    @observe(name="anthropic.chat")
    async def chat(...):
        ...

    # Within an @observe()-wrapped function, capture a nested LLM call:
    from langfuse.decorators import langfuse_context
    langfuse_context.update_current_observation(
        model="claude-haiku-4-5-20251001",
        usage={"input": prompt_tokens, "output": completion_tokens},
    )
"""

import logging
from functools import wraps
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# ── Langfuse client singleton ──────────────────────────────────────────────────

_langfuse_client = None
_langfuse_enabled = False


def _get_langfuse_client():
    """Return the Langfuse client, or None if observability is disabled."""
    global _langfuse_client, _langfuse_enabled
    if _langfuse_client is not None or not _langfuse_enabled:
        return _langfuse_client
    return _langfuse_client


def init_langfuse() -> bool:
    """
    Initialise the Langfuse SDK using settings from app.core.config.

    Called once during FastAPI lifespan startup.  Safe to call multiple times.
    Returns True if initialisation succeeded, False otherwise.
    """
    global _langfuse_client, _langfuse_enabled

    try:
        from app.core.config import settings

        # Skip if credentials are not configured
        if not settings.LANGFUSE_PUBLIC_KEY or not settings.LANGFUSE_SECRET_KEY:
            logger.info("Langfuse credentials not set — observability disabled")
            _langfuse_enabled = False
            return False

        from langfuse import Langfuse

        _langfuse_client = Langfuse(
            public_key=settings.LANGFUSE_PUBLIC_KEY,
            secret_key=settings.LANGFUSE_SECRET_KEY,
            host=settings.LANGFUSE_HOST,
        )

        # Verify connectivity
        _langfuse_client.auth_check()
        _langfuse_enabled = True
        logger.info("Langfuse observability initialised — host: %s", settings.LANGFUSE_HOST)
        return True

    except Exception as exc:
        logger.warning("Langfuse init failed — observability disabled: %s", exc)
        _langfuse_enabled = False
        _langfuse_client = None
        return False


def shutdown_langfuse() -> None:
    """Flush pending events to Langfuse before process exit."""
    global _langfuse_client
    if _langfuse_client is not None:
        try:
            _langfuse_client.flush()
            logger.info("Langfuse: flushed pending traces")
        except Exception as exc:
            logger.warning("Langfuse flush error: %s", exc)


# ── Decorator ─────────────────────────────────────────────────────────────────


def observe(
    name: Optional[str] = None,
    *,
    capture_input: bool = True,
    capture_output: bool = True,
):
    """
    Decorator that wraps an async function as a Langfuse trace/span.

    When Langfuse is disabled the function runs unchanged — zero overhead.

    Parameters
    ----------
    name:
        Human-readable name shown in the Langfuse trace tree.
        Defaults to the wrapped function's qualified name.
    capture_input:
        Whether to log function arguments as the trace input.
    capture_output:
        Whether to log the return value as the trace output.
    """
    def decorator(fn: Callable) -> Callable:
        trace_name = name or f"{fn.__module__}.{fn.__qualname__}"

        @wraps(fn)
        async def wrapper(*args, **kwargs):
            if not _langfuse_enabled or _langfuse_client is None:
                return await fn(*args, **kwargs)

            # ── Build a safe, serialisable input dict ──────────────────────
            safe_input: dict[str, Any] = {}
            if capture_input:
                try:
                    import inspect
                    sig = inspect.signature(fn)
                    bound = sig.bind(*args, **kwargs)
                    bound.apply_defaults()
                    safe_input = {
                        k: _safe_repr(v) for k, v in bound.arguments.items()
                    }
                except Exception:
                    safe_input = {}

            trace = _langfuse_client.trace(
                name=trace_name,
                input=safe_input if capture_input else None,
                metadata=_extract_trace_metadata(safe_input),
            )

            try:
                result = await fn(*args, **kwargs)
                if capture_output and trace is not None:
                    try:
                        trace.update(output=_safe_repr(result))
                    except Exception:
                        pass
                return result

            except Exception as exc:
                if trace is not None:
                    try:
                        trace.update(
                            level="ERROR",
                            status_message=str(exc),
                        )
                    except Exception:
                        pass
                raise

            finally:
                # Non-blocking flush — Langfuse SDK batches in background
                pass

        return wrapper
    return decorator


def observe_tool_call(trace_id: Optional[str], tool_name: str, tool_input: dict, tool_output: dict) -> None:
    """
    Record a single tool call as a child span on an existing trace.

    Call this immediately after executing a tool inside an @observe()-wrapped function.

    Parameters
    ----------
    trace_id:
        The parent trace ID (obtain via langfuse_context.get_current_trace_id() inside
        an @observe()-decorated function, if using langfuse.decorators).
        If None or Langfuse is disabled, this is a no-op.
    tool_name:
        Name of the tool (e.g. "search_knowledge_base").
    tool_input:
        Arguments passed to the tool.
    tool_output:
        Result returned by the tool.
    """
    if not _langfuse_enabled or _langfuse_client is None or trace_id is None:
        return
    try:
        _langfuse_client.span(
            trace_id=trace_id,
            name=f"tool:{tool_name}",
            input=_safe_repr(tool_input),
            output=_safe_repr(tool_output),
        )
    except Exception as exc:
        logger.debug("Langfuse tool span error: %s", exc)


def create_llm_generation(
    trace_id: str,
    *,
    model: str,
    provider: str,
    system_prompt: str = "",
    user_message: str = "",
    completion: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    latency_ms: float = 0.0,
    metadata: Optional[dict] = None,
) -> None:
    """
    Record a single LLM generation (one model call) as a child generation on a trace.

    Parameters
    ----------
    trace_id:
        Parent trace ID.
    model:
        Model identifier (e.g. "claude-haiku-4-5-20251001").
    provider:
        Provider name ("anthropic", "gemini", "openrouter").
    system_prompt, user_message, completion:
        The prompt/response content.
    input_tokens, output_tokens:
        Token counts from the model response.
    latency_ms:
        Wall-clock time for the model call in milliseconds.
    metadata:
        Any additional key/value pairs to store.
    """
    if not _langfuse_enabled or _langfuse_client is None:
        return
    try:
        _langfuse_client.generation(
            trace_id=trace_id,
            name=f"{provider}.completion",
            model=model,
            model_parameters={"provider": provider},
            input=[
                {"role": "system", "content": system_prompt[:2000]},
                {"role": "user", "content": user_message[:2000]},
            ],
            output=completion[:4000],
            usage={
                "input": input_tokens,
                "output": output_tokens,
                "total": input_tokens + output_tokens,
            },
            metadata=metadata or {},
        )
    except Exception as exc:
        logger.debug("Langfuse generation error: %s", exc)


# ── Internal helpers ───────────────────────────────────────────────────────────


def _safe_repr(value: Any, max_len: int = 4000) -> Any:
    """
    Convert a value to something JSON-serialisable and bounded in size.
    Strips MongoDB ObjectId and other unserializable types.
    """
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        v = str(value)
        return v[:max_len] if len(v) > max_len else value
    if isinstance(value, dict):
        return {str(k): _safe_repr(v, max_len // max(1, len(value))) for k, v in list(value.items())[:20]}
    if isinstance(value, (list, tuple)):
        return [_safe_repr(item, max_len // max(1, len(value))) for item in list(value)[:20]]
    return str(value)[:max_len]


def _extract_trace_metadata(safe_input: dict) -> dict:
    """Pull well-known keys from function args to use as top-level trace metadata."""
    meta = {}
    for key in ("company_id", "session_id", "user_id", "user_message", "page_url"):
        if key in safe_input:
            meta[key] = safe_input[key]
    return meta


# Public re-export so callers don't need to import from two places
__all__ = [
    "init_langfuse",
    "shutdown_langfuse",
    "observe",
    "observe_tool_call",
    "create_llm_generation",
]
