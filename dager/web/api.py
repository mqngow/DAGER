"""FastAPI backend for ticker/company search, S&P 500 leaderboard, and on-demand evaluation."""

from contextlib import asynccontextmanager
import logging
from pathlib import Path
from typing import List, Optional
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from dager.config import configure_logging
from dager.schemas.evaluation import StockEvaluationReport
from dager.storage import EvaluationRepo, get_session_factory, init_db
from flows.evaluation_flow import (
    daily_sp500_evaluation_flow,
    evaluate_single_company_flow,
)

# Ensure `logs/dager.log` + console handlers are attached even when started directly via Uvicorn
configure_logging()
logger = logging.getLogger("dager.web")

UI_HTML_PATH = Path(__file__).parent / "ui" / "index.html"


# ==============================================================================
# FastAPI Application & Database Session Dependency
# Initializes SQLite/PostgreSQL tables on startup and yields a scoped SQLAlchemy
# session per HTTP request.
# ==============================================================================
@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Ensure all database tables exist when the API server boots."""
    logger.info("DAGER FastAPI server starting up; initializing database tables.")
    init_db()
    yield


app = FastAPI(
    title="DAGER — Investment Research & Decision Engine",
    description=(
        "Multi-horizon (1m, 3m, 6m, 1y, 3y, 5y) stock evaluation engine combining "
        "SEC EDGAR XBRL balance sheets, DCF/multiple valuation, and FinBERT news sentiment."
    ),
    version="0.1.0",
    lifespan=_lifespan,
)


def get_db_session():
    """FastAPI dependency that yields a transactional SQLAlchemy Session."""
    init_db()
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ==============================================================================
# 1. Frontend Dashboard Route (`GET /`)
# Serves the single-page DAGER research dashboard (`dager/web/ui/index.html`).
# ==============================================================================
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def serve_dashboard() -> HTMLResponse:
    """Serve the interactive DAGER web dashboard."""
    if not UI_HTML_PATH.exists():
        return HTMLResponse("<h1>DAGER API is running. UI file not found.</h1>", status_code=200)
    return HTMLResponse(UI_HTML_PATH.read_text(encoding="utf-8"))


# ==============================================================================
# 2. Health & System Status Endpoint (`GET /api/health`)
# Confirms API readiness and supported evaluation horizons.
# ==============================================================================
@app.get("/api/health")
def health_check() -> dict:
    """Return service health status and supported investment horizons."""
    return {
        "status": "ok",
        "service": "DAGER",
        "supported_horizons": ["1m", "3m", "6m", "1y", "3y", "5y", "6m-3y"],
    }


# ==============================================================================
# 3. Ranked S&P 500 Leaderboard Endpoint (`GET /api/sp500`)
# Returns pre-computed evaluations stored in the database, optionally re-sorted
# by a specific horizon (`1m`, `3m`, `6m`, `1y`, `3y`, `5y`).
# ==============================================================================
@app.get("/api/sp500", response_model=List[StockEvaluationReport])
def list_sp500_evaluations(
    limit: int = Query(default=50, ge=1, le=500, description="Max companies to return."),
    sp500_only: bool = Query(default=False, description="If False, includes all stored evaluations."),
    sort_by: str = Query(default="score", description="Sort order: 'score' or 'recent'."),
    horizon: Optional[str] = Query(
        default=None,
        description="Optional horizon key ('1m', '3m', '6m', '1y', '3y', '5y') to sort leaderboard by.",
    ),
    session: Session = Depends(get_db_session),
) -> List[StockEvaluationReport]:
    """Return stored company evaluations ranked by composite/horizon score or most recently evaluated."""
    repo = EvaluationRepo(session)
    reports = repo.list_top_evaluations(sp500_only=sp500_only, limit=limit, sort_by=sort_by)

    if sort_by != "recent" and horizon and horizon in ("1m", "3m", "6m", "1y", "3y", "5y"):
        reports.sort(
            key=lambda r: r.horizon_signals[horizon].score if horizon in r.horizon_signals else r.composite_score,
            reverse=True,
        )
    return reports


# ==============================================================================
# 4. On-Demand Company Lookup & Evaluation Endpoint (`GET /api/evaluate/{query}`)
# Accepts ANY ticker (`AAPL`) or company name (`Apple`, `Microsoft`).
# - Cache Hit: Returns stored report immediately (`was_cached=True`).
# - Cache Miss / Force Refresh: Dynamically triggers `evaluate_single_company_flow`
#   to pull SEC EDGAR filings, market prices, and news sentiment, persists to DB,
#   and returns the complete multi-horizon report (`was_cached=False`).
# ==============================================================================
@app.get("/api/evaluate/{query}", response_model=StockEvaluationReport)
def evaluate_ticker_or_company(
    query: str,
    force_refresh: bool = Query(default=False, description="Bypass DB cache and recompute live."),
    include_transcripts: bool = Query(
        default=False,
        description="Include SEC 8-K/10-K/10-Q narrative excerpts in sentiment analysis.",
    ),
) -> StockEvaluationReport:
    """Retrieve a cached evaluation or trigger an on-demand evaluation for any ticker or company name."""
    cleaned = query.strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Query cannot be empty.")

    logger.info("Web API evaluate request: query='%s', force_refresh=%s", cleaned, force_refresh)
    try:
        rep = evaluate_single_company_flow(
            query=cleaned,
            force_refresh=force_refresh,
            include_transcripts=include_transcripts,
        )
        logger.info(
            "Web API evaluate success: %s (%s) | Verdict=%s | Score=%.1f | Cached=%s",
            rep.company_name,
            rep.ticker,
            rep.verdict.value,
            rep.composite_score,
            rep.was_cached,
        )
        return rep
    except KeyError as exc:
        logger.warning("Web API ticker resolution failed for '%s': %s", cleaned, exc)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Web API evaluation error for '%s'", cleaned)
        raise HTTPException(status_code=500, detail=f"Evaluation failed for '{cleaned}': {exc}") from exc


# ==============================================================================
# 5. Batch Seed Endpoint (`POST /api/sp500/seed`)
# Allows triggering a quick batch evaluation of top S&P 500 names directly from
# the UI or API if the local database is empty.
# ==============================================================================
@app.post("/api/sp500/seed", response_model=List[StockEvaluationReport])
def seed_sp500_sample(
    limit: int = Query(default=5, ge=1, le=50, description="Number of S&P 500 companies to evaluate."),
) -> List[StockEvaluationReport]:
    """Run the S&P 500 batch evaluation flow for `limit` companies and populate the database."""
    logger.info("Web API batch seed requested (limit=%d)", limit)
    sample_tickers = (
        ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "BRK-B", "LLY", "AVGO", "JPM"][:limit]
        if limit <= 10
        else None
    )
    try:
        reports = daily_sp500_evaluation_flow(
            tickers=sample_tickers,
            limit=limit,
            force_refresh=False,
            include_transcripts=False,
        )
        logger.info("Web API batch seed completed (%d companies evaluated).", len(reports))
        return reports
    except Exception as exc:
        logger.exception("Web API batch seed failed (limit=%d)", limit)
        raise HTTPException(status_code=500, detail=f"Batch seed failed: {exc}") from exc
