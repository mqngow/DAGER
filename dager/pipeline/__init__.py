"""Modular Prefect tasks with zero-overhead fallback when Prefect runtime is not active."""

import functools
import logging
import os
from typing import Any, Callable, TypeVar
import dager.config  # noqa: F401 - ensures `.env` is loaded into `os.environ` before Prefect initializes

F = TypeVar("F", bound=Callable[..., Any])
logger = logging.getLogger("dager.pipeline")


# ==============================================================================
# Prefect `@task` & `@flow` Compatibility Wrappers
# Uses real `prefect.task` and `prefect.flow` decorators when `PREFECT_API_URL`
# is configured, and runs directly in-process when unset (e.g., on Cloud Run)
# so Prefect never attempts to spawn an ephemeral SubprocessASGIServer.
# ==============================================================================
try:
    from prefect import flow as _real_flow, task as _real_task  # type: ignore

    def _make_safe_prefect_wrapper(real_decorator: Any, *args: Any, **kwargs: Any) -> Callable[[F], F]:
        prefect_factory = real_decorator(*args, **kwargs)

        def _decorator(fn: F) -> F:
            decorated = prefect_factory(fn)

            @functools.wraps(fn)
            def _safe_runner(*call_args: Any, **call_kwargs: Any) -> Any:
                if not os.environ.get("PREFECT_API_URL"):
                    return fn(*call_args, **call_kwargs)
                try:
                    return decorated(*call_args, **call_kwargs)
                except Exception as exc:
                    exc_msg = str(exc)
                    exc_mod = type(exc).__module__ or ""
                    if (
                        "Server process did not start" in exc_msg
                        or exc_mod.startswith("prefect")
                    ):
                        logger.warning(
                            "Prefect control-plane error (%s); executing '%s' in-process.",
                            exc_msg,
                            fn.__name__,
                        )
                        return fn(*call_args, **call_kwargs)
                    raise

            setattr(_safe_runner, "fn", fn)
            for attr in ("serve", "deploy", "with_options"):
                if hasattr(decorated, attr):
                    setattr(_safe_runner, attr, getattr(decorated, attr))
            return _safe_runner  # type: ignore[return-value]

        return _decorator

    def prefect_task(*args: Any, **kwargs: Any) -> Callable[[F], F]:
        """Wrap a function as a Prefect `@task` when `PREFECT_API_URL` is configured."""
        kwargs.setdefault("log_prints", True)
        return _make_safe_prefect_wrapper(_real_task, *args, **kwargs)

    def prefect_flow(*args: Any, **kwargs: Any) -> Callable[[F], F]:
        """Wrap a function as a Prefect `@flow` when `PREFECT_API_URL` is configured."""
        kwargs.setdefault("log_prints", True)
        return _make_safe_prefect_wrapper(_real_flow, *args, **kwargs)

    PREFECT_AVAILABLE = True
except Exception:
    PREFECT_AVAILABLE = False

    def prefect_task(*args: Any, **kwargs: Any) -> Callable[[F], F]:
        """Fallback no-op `@task` decorator when Prefect is not installed."""
        def _decorator(fn: F) -> F:
            setattr(fn, "fn", fn)
            return fn
        return _decorator

    def prefect_flow(*args: Any, **kwargs: Any) -> Callable[[F], F]:
        """Fallback no-op `@flow` decorator when Prefect is not installed."""
        def _decorator(fn: F) -> F:
            setattr(fn, "fn", fn)
            return fn
        return _decorator


from dager.pipeline.tasks_evaluation import (  # noqa: E402
    check_cached_evaluation_task,
    persist_evaluation_task,
    synthesize_evaluation_task,
)
from dager.pipeline.tasks_fundamentals import (  # noqa: E402
    collect_and_parse_financials_task,
    compute_fundamentals_task,
    fetch_sp500_universe_task,
    persist_financials_task,
    resolve_company_task,
)
from dager.pipeline.tasks_sentiment import (  # noqa: E402
    collect_sentiment_documents_task,
    persist_sentiment_task,
    score_company_sentiment_task,
)

__all__ = [
    "PREFECT_AVAILABLE",
    "prefect_task",
    "prefect_flow",
    "resolve_company_task",
    "fetch_sp500_universe_task",
    "collect_and_parse_financials_task",
    "compute_fundamentals_task",
    "persist_financials_task",
    "collect_sentiment_documents_task",
    "score_company_sentiment_task",
    "persist_sentiment_task",
    "check_cached_evaluation_task",
    "synthesize_evaluation_task",
    "persist_evaluation_task",
]

