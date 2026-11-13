"""Orchestrated Prefect workflows for Daily S&P 500 Batch & On-Demand Single-Company Evaluation."""

import argparse
import logging
from typing import Any, Callable, List, Optional
from sqlalchemy.engine import Engine

from dager.collectors import EdgarClient, MarketClient, NewsClient, TranscriptClient
from dager.config import configure_logging
from dager.core.sentiment import SentimentScorer
from dager.pipeline import (
    check_cached_evaluation_task,
    collect_and_parse_financials_task,
    collect_sentiment_documents_task,
    fetch_sp500_universe_task,
    persist_evaluation_task,
    persist_financials_task,
    persist_sentiment_task,
    prefect_flow,
    resolve_company_task,
    score_company_sentiment_task,
    synthesize_evaluation_task,
)
from dager.schemas.evaluation import StockEvaluationReport
from dager.storage import EvaluationRepo, init_db, session_scope
from flows.fundamentals_flow import ingest_company_fundamentals_flow  # noqa: F401
from flows.sentiment_flow import ingest_company_sentiment_flow  # noqa: F401

configure_logging()
logger = logging.getLogger("dager.flows")


# ==============================================================================
# 1. On-Demand Single-Company Evaluation Flow (Cache-Miss Fallback)
# Triggered when a user searches any ticker or company name:
# - Step 1: Checks the relational DB cache (`check_cached_evaluation_task`).
#   If a fresh evaluation exists within TTL, returns it immediately (`was_cached=True`).
# - Step 2 (Cache Miss): Resolves company via SEC EDGAR, pulls balance sheets,
#   income/cash flows, 5y stock prices, and news/transcripts, computes the
#   6m–3y investment thesis, persists all records, and returns the report (`was_cached=False`).
# ==============================================================================
@prefect_flow(name="evaluate-single-company")
def evaluate_single_company_flow(
    query: str,
    force_refresh: bool = False,
    include_transcripts: bool = True,
    use_finbert: bool = True,
    engine: Optional[Engine] = None,
    edgar_client: Optional[EdgarClient] = None,
    market_client: Optional[MarketClient] = None,
    news_client: Optional[NewsClient] = None,
    transcript_client: Optional[TranscriptClient] = None,
    scorer: Optional[SentimentScorer] = None,
) -> StockEvaluationReport:
    """Evaluate any company by ticker or name, serving from cache if fresh or computing live on miss."""
    if not force_refresh:
        cached = check_cached_evaluation_task(query, engine=engine)
        if cached is not None:
            return cached

    # Live On-Demand Ingestion & Analysis
    identity = resolve_company_task(query, edgar_client=edgar_client)

    financials = collect_and_parse_financials_task(
        company=identity,
        edgar_client=edgar_client,
        market_client=market_client,
    )
    persist_financials_task(financials, engine=engine)

    docs = collect_sentiment_documents_task(
        company=identity,
        include_transcripts=include_transcripts,
        news_client=news_client,
        transcript_client=transcript_client,
    )
    sentiment = score_company_sentiment_task(
        ticker=identity.ticker,
        documents=docs,
        use_finbert=use_finbert,
        scorer=scorer,
    )
    persist_sentiment_task(identity, sentiment, engine=engine)

    report = synthesize_evaluation_task(
        financials=financials,
        sentiment=sentiment,
        was_cached=False,
    )
    persist_evaluation_task(report, engine=engine)
    return report


# ==============================================================================
# 2. Scheduled Daily S&P 500 Batch Evaluation Flow
# Iterates across the S&P 500 universe, refreshes fundamentals, prices, and
# news sentiment, stores evaluations in the database, and returns the ranked
# leaderboard sorted by `composite_score` descending.
# ==============================================================================
def _run_task_fn(task_obj: Any, *args: Any, **kwargs: Any) -> Any:
    """Invoke a Prefect task's underlying `.fn` directly to avoid nested Prefect API overhead."""
    fn = getattr(task_obj, "fn", task_obj)
    return fn(*args, **kwargs)


def _evaluate_ticker_fast(
    query: str,
    include_transcripts: bool = False,
    use_finbert: bool = True,
    engine: Optional[Engine] = None,
    edgar_client: Optional[EdgarClient] = None,
    market_client: Optional[MarketClient] = None,
    news_client: Optional[NewsClient] = None,
    transcript_client: Optional[TranscriptClient] = None,
    scorer: Optional[SentimentScorer] = None,
) -> StockEvaluationReport:
    """Execute single-company ingestion, scoring, and persistence directly via task `.fn` calls."""
    identity = _run_task_fn(resolve_company_task, query, edgar_client=edgar_client)
    if not identity.is_sp500:
        identity = identity.model_copy(update={"is_sp500": True})

    financials = _run_task_fn(
        collect_and_parse_financials_task,
        company=identity,
        edgar_client=edgar_client,
        market_client=market_client,
    )
    _run_task_fn(persist_financials_task, financials, engine=engine)

    docs = _run_task_fn(
        collect_sentiment_documents_task,
        company=identity,
        include_transcripts=include_transcripts,
        news_client=news_client,
        transcript_client=transcript_client,
    )
    sentiment = _run_task_fn(
        score_company_sentiment_task,
        ticker=identity.ticker,
        documents=docs,
        use_finbert=use_finbert,
        scorer=scorer,
    )
    _run_task_fn(persist_sentiment_task, identity, sentiment, engine=engine)

    report = _run_task_fn(
        synthesize_evaluation_task,
        financials=financials,
        sentiment=sentiment,
        was_cached=False,
    )
    if not report.is_sp500:
        report = report.model_copy(update={"is_sp500": True})
    _run_task_fn(persist_evaluation_task, report, engine=engine)
    return report


@prefect_flow(name="daily-sp500-evaluation")
def daily_sp500_evaluation_flow(
    tickers: Optional[List[str]] = None,
    limit: Optional[int] = None,
    force_refresh: bool = False,
    include_transcripts: bool = False,
    use_finbert: bool = True,
    engine: Optional[Engine] = None,
    max_passes: int = 2,
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
) -> List[StockEvaluationReport]:
    """Run resumable daily S&P 500 evaluation, skipping fresh (<24h) tickers and retrying misses."""
    init_db(engine)

    edgar = EdgarClient()
    market = MarketClient()
    news = NewsClient()
    raw_universe = tickers or _run_task_fn(fetch_sp500_universe_task, limit=limit, edgar_client=edgar)
    universe = [t.strip().upper() for t in raw_universe if t and t.strip()]
    edgar.sp500_tickers.update(universe)

    reports_by_ticker: dict[str, StockEvaluationReport] = {}
    with session_scope(engine) as session:
        repo = EvaluationRepo(session)
        repo.ensure_sp500_flag(set(universe))
        already_fresh = set() if force_refresh else repo.get_fresh_tickers()
        for symbol in universe:
            if symbol in already_fresh:
                cached = repo.get_fresh_evaluation(symbol)
                if cached is not None:
                    reports_by_ticker[symbol] = cached

    completed_tickers: set[str] = set(reports_by_ticker.keys())
    total_count = len(universe)

    for pass_num in range(1, max(1, max_passes) + 1):
        remaining = [s for s in universe if s not in completed_tickers]
        if not remaining:
            break
        logger.info(
            "S&P 500 daily batch pass %d/%d: evaluating %d remaining of %d tickers.",
            pass_num,
            max_passes,
            len(remaining),
            total_count,
        )
        for symbol in remaining:
            if progress_callback is not None:
                progress_callback(symbol, len(completed_tickers), total_count)
            try:
                rep = _evaluate_ticker_fast(
                    query=symbol,
                    include_transcripts=include_transcripts,
                    use_finbert=use_finbert,
                    engine=engine,
                    edgar_client=edgar,
                    market_client=market,
                    news_client=news,
                )
                completed_tickers.add(symbol)
                reports_by_ticker[symbol] = rep
                if progress_callback is not None:
                    progress_callback(symbol, len(completed_tickers), total_count)
            except Exception:
                logger.exception(
                    "Failed evaluating ticker '%s' during batch pass %d/%d",
                    symbol,
                    pass_num,
                    max_passes,
                )
                continue

    reports = list(reports_by_ticker.values())
    reports.sort(key=lambda r: r.composite_score, reverse=True)
    return reports


# ==============================================================================
# 3. CLI & Scheduled Cloud Deployment Entrypoint
# Allows running a single batch execution (`python -m flows.evaluation_flow --limit 20`)
# via GitHub Actions / cloud cron jobs, or serving a persistent Prefect schedule
# (`python -m flows.evaluation_flow --serve --cron "0 6 * * *"`).
# ==============================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DAGER S&P 500 & On-Demand Evaluation Flow Runner")
    parser.add_argument("--ticker", type=str, help="Evaluate a single ticker or company name on-demand.")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of S&P 500 companies in batch run.")
    parser.add_argument("--serve", action="store_true", help="Serve scheduled daily Prefect deployment.")
    parser.add_argument("--cron", type=str, default="0 6 * * *", help="Cron schedule (default: 06:00 UTC daily).")
    args = parser.parse_args()

    if args.serve and hasattr(daily_sp500_evaluation_flow, "serve"):
        daily_sp500_evaluation_flow.serve(
            name="dager-daily-sp500-deployment",
            cron=args.cron,
        )
    elif args.ticker:
        result = evaluate_single_company_flow(args.ticker)
        print(f"[{result.ticker}] {result.verdict.value} ({result.composite_score}/100): {result.thesis_summary}")
    else:
        batch_results = daily_sp500_evaluation_flow(limit=args.limit or 10)
        print(f"Completed batch evaluation of {len(batch_results)} companies.")

