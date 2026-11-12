"""Modular Prefect tasks with zero-overhead fallback when Prefect runtime is not active."""

from typing import Any, Callable, TypeVar
import dager.config  # noqa: F401 - ensures `.env` is loaded into `os.environ` before Prefect initializes

F = TypeVar("F", bound=Callable[..., Any])


# ==============================================================================
# Prefect `@task` & `@flow` Compatibility Wrappers
# Uses real `prefect.task` and `prefect.flow` decorators when Prefect is
# installed, while preserving `.fn` access and clean execution in lightweight
# test or serverless cron environments.
# ==============================================================================
try:
    from prefect import flow as _real_flow, task as _real_task  # type: ignore

    def prefect_task(*args: Any, **kwargs: Any) -> Callable[[F], F]:
        """Wrap a function as a Prefect `@task`."""
        return _real_task(*args, **kwargs)

    def prefect_flow(*args: Any, **kwargs: Any) -> Callable[[F], F]:
        """Wrap a function as a Prefect `@flow`."""
        return _real_flow(*args, **kwargs)

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

