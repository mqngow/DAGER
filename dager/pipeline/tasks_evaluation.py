"""Prefect tasks for metric synthesis, composite 6m–3y scoring, and cache-miss fallback."""

from typing import Optional
from sqlalchemy.engine import Engine

from dager.core.evaluator import evaluate_company
from dager.pipeline import prefect_task
from dager.schemas.evaluation import StockEvaluationReport
from dager.schemas.filings import CompanyFinancials
from dager.schemas.sentiment import CompanySentimentSummary
from dager.storage import EvaluationRepo, init_db, session_scope


# ==============================================================================
# 1. Multi-Factor Evaluation Synthesis Task
# Combines `CompanyFinancials` (Pillars 1 & 2) and `CompanySentimentSummary`
# (Pillar 3) into a final `StockEvaluationReport`.
# ==============================================================================
@prefect_task(name="synthesize-stock-evaluation")
def synthesize_evaluation_task(
    financials: CompanyFinancials,
    sentiment: Optional[CompanySentimentSummary] = None,
    was_cached: bool = False,
) -> StockEvaluationReport:
    """Compute composite 0–100 score, verdict, and 1m–5y thesis from financials and sentiment."""
    return evaluate_company(
        financials=financials,
        sentiment=sentiment,
        was_cached=was_cached,
    )


# ==============================================================================
# 2. Evaluation Persistence & Cache Lookup Tasks
# Checks relational storage for a fresh report within TTL (`check_cached_evaluation_task`)
# and persists newly computed evaluations (`persist_evaluation_task`).
# ==============================================================================
@prefect_task(name="check-cached-evaluation")
def check_cached_evaluation_task(
    query: str,
    engine: Optional[Engine] = None,
) -> Optional[StockEvaluationReport]:
    """Return a cached `StockEvaluationReport` (`was_cached=True`) if fresh in DB, else `None`."""
    init_db(engine)
    with session_scope(engine) as session:
        repo = EvaluationRepo(session)
        return repo.get_fresh_evaluation(query)


@prefect_task(name="persist-stock-evaluation")
def persist_evaluation_task(
    report: StockEvaluationReport,
    engine: Optional[Engine] = None,
) -> int:
    """Save a `StockEvaluationReport` to the `evaluation_results` table and return its ID."""
    init_db(engine)
    with session_scope(engine) as session:
        repo = EvaluationRepo(session)
        row = repo.save_evaluation(report)
        return row.id

