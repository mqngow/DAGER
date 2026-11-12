"""Orchestrated Prefect workflows for Daily S&P 500 Batch & On-Demand Single-Company Evaluation."""

import argparse
from typing import List, Optional
from sqlalchemy.engine import Engine

from dager.collectors import EdgarClient, MarketClient, NewsClient, TranscriptClient
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
from flows.fundamentals_flow import ingest_company_fundamentals_flow  # noqa: F401
from flows.sentiment_flow import ingest_company_sentiment_flow  # noqa: F401


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
@prefect_flow(name="daily-sp500-evaluation")
def daily_sp500_evaluation_flow(
    tickers: Optional[List[str]] = None,
    limit: Optional[int] = None,
    force_refresh: bool = True,
    include_transcripts: bool = False,
    use_finbert: bool = True,
    engine: Optional[Engine] = None,
) -> List[StockEvaluationReport]:
    """Run the scheduled S&P 500 batch evaluation pipeline and return ranked reports."""
    edgar = EdgarClient()
    market = MarketClient()
    news = NewsClient()
    transcripts = TranscriptClient(edgar_client=edgar)
    scorer = SentimentScorer(use_finbert=use_finbert)

    universe = tickers or fetch_sp500_universe_task(limit=limit, edgar_client=edgar)
    edgar.sp500_tickers.update(universe)

    reports: List[StockEvaluationReport] = []
    for symbol in universe:
        try:
            rep = evaluate_single_company_flow(
                query=symbol,
                force_refresh=force_refresh,
                include_transcripts=include_transcripts,
                use_finbert=use_finbert,
                engine=engine,
                edgar_client=edgar,
                market_client=market,
                news_client=news,
                transcript_client=transcripts,
                scorer=scorer,
            )
            reports.append(rep)
        except Exception:
            continue

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

