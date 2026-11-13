"""Master Prefect Pipeline & Application Entrypoint (`main.py`)."""

import argparse
from typing import List, Optional
import uvicorn

from dager.config import configure_logging, settings
from dager.pipeline import prefect_flow
from dager.schemas.evaluation import StockEvaluationReport
from dager.storage import init_db
from flows.evaluation_flow import (
    daily_sp500_evaluation_flow,
    evaluate_single_company_flow,
)
from flows.fundamentals_flow import ingest_company_fundamentals_flow
from flows.sentiment_flow import ingest_company_sentiment_flow

# Initialize console + rotating file logger (`logs/dager.log`) immediately
logger = configure_logging()


# ==============================================================================
# Master End-to-End Prefect Pipeline Flow (`dager_master_pipeline_flow`)
# Orchestrates the entire DAGER workflow in one top-level Prefect flow:
# 1. Initializes the relational database (Supabase PostgreSQL or local SQLite).
# 2. Runs either a targeted single-company flow (`ticker`) or the S&P 500 batch
#    flow (`fundamentals_flow` -> `sentiment_flow` -> `evaluation_flow`).
# 3. Logs every step to both `logs/dager.log` and Prefect Cloud (`log_prints=True`).
# ==============================================================================
@prefect_flow(name="dager-master-pipeline")
def dager_master_pipeline_flow(
    ticker: Optional[str] = None,
    limit: int = 503,
    force_refresh: bool = False,
    include_transcripts: bool = False,
    use_finbert: bool = True,
) -> List[StockEvaluationReport]:
    """Execute the complete DAGER Prefect pipeline and collect structured logs."""
    logger.info("Starting DAGER Master Prefect Pipeline (ticker=%s, limit=%s)", ticker, limit)
    init_db()

    if ticker:
        logger.info("Running single-company Prefect flow for query='%s'", ticker)
        report = evaluate_single_company_flow(
            query=ticker,
            force_refresh=force_refresh,
            include_transcripts=include_transcripts,
            use_finbert=use_finbert,
        )
        logger.info(
            "Completed evaluation for %s (%s) | Verdict=%s | Score=%.1f/100 | Cached=%s",
            report.company_name,
            report.ticker,
            report.verdict.value,
            report.composite_score,
            report.was_cached,
        )
        return [report]

    logger.info("Running S&P 500 batch Prefect flow (limit=%d, force_refresh=%s)", limit, force_refresh)
    reports = daily_sp500_evaluation_flow(
        limit=limit,
        force_refresh=force_refresh,
        include_transcripts=include_transcripts,
        use_finbert=use_finbert,
    )
    logger.info("Finished S&P 500 batch flow: %d companies evaluated and stored.", len(reports))
    for rep in reports:
        logger.info(
            "  [%s] 6m-3y=%s (%.1f) | 1m=%s | 3m=%s | 6m=%s | 1y=%s | 3y=%s | 5y=%s",
            rep.ticker,
            rep.verdict.value,
            rep.composite_score,
            rep.horizon_signals["1m"].verdict.value if "1m" in rep.horizon_signals else "-",
            rep.horizon_signals["3m"].verdict.value if "3m" in rep.horizon_signals else "-",
            rep.horizon_signals["6m"].verdict.value if "6m" in rep.horizon_signals else "-",
            rep.horizon_signals["1y"].verdict.value if "1y" in rep.horizon_signals else "-",
            rep.horizon_signals["3y"].verdict.value if "3y" in rep.horizon_signals else "-",
            rep.horizon_signals["5y"].verdict.value if "5y" in rep.horizon_signals else "-",
        )
    return reports


# ==============================================================================
# CLI Runner (`python main.py`)
# Default: Runs the full `dager_master_pipeline_flow` and logs to `logs/dager.log`
# and Prefect Cloud.
# Optional flags:
# - `python main.py --ticker AAPL`: Run full Prefect flow for a single company.
# - `python main.py --limit 10`: Run full Prefect batch flow for 10 S&P 500 companies.
# - `python main.py --serve`: Register & serve daily cron deployment in Prefect Cloud.
# - `python main.py --web`: Launch the FastAPI Web Dashboard at http://127.0.0.1:8000.
# ==============================================================================
def main() -> None:
    parser = argparse.ArgumentParser(description="DAGER — Master Prefect Pipeline & Web Server")
    parser.add_argument("--ticker", type=str, default=None, help="Run the Prefect pipeline for a single ticker/company.")
    parser.add_argument("--limit", type=int, default=503, help="Number of S&P 500 companies for batch Prefect run (default: 503).")
    parser.add_argument("--force-refresh", action="store_true", help="Bypass DB cache and recompute live.")
    parser.add_argument("--serve", action="store_true", help="Serve scheduled daily Prefect deployment (cron).")
    parser.add_argument("--cron", type=str, default="0 6 * * *", help="Cron schedule for --serve (default: '0 6 * * *').")
    parser.add_argument("--web", action="store_true", help="Start the FastAPI Web Dashboard instead of a batch run.")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Web server host (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8000, help="Web server port (default: 8000).")
    args = parser.parse_args()

    if args.web:
        logger.info("Launching DAGER FastAPI Web Dashboard at http://%s:%d", args.host, args.port)
        uvicorn.run("dager.web.api:app", host=args.host, port=args.port, reload=False)
        return

    if args.serve and hasattr(dager_master_pipeline_flow, "serve"):
        logger.info("Serving scheduled Prefect deployment 'dager-daily-master' on cron='%s'", args.cron)
        dager_master_pipeline_flow.serve(
            name="dager-daily-master",
            cron=args.cron,
            parameters={"limit": args.limit, "force_refresh": True},
        )
        return

    # Default action: execute the full Prefect pipeline flow
    dager_master_pipeline_flow(
        ticker=args.ticker,
        limit=args.limit,
        force_refresh=args.force_refresh,
    )


if __name__ == "__main__":
    main()
