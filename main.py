"""Main application entrypoint for starting the DAGER web server or running evaluations."""

import argparse
from dotenv import load_dotenv
import uvicorn

from dager.config import configure_logging, settings


# ==============================================================================
# DAGER Main Application Entrypoint (`python main.py`)
# Loads `.env` variables (including `PREFECT_API_KEY`, `PREFECT_API_URL`, and
# `DATABASE_URL`) and either:
# 1. Starts the FastAPI Web Dashboard & API server on `http://127.0.0.1:8000` (default).
# 2. Runs an on-demand ticker evaluation (`python main.py --ticker AAPL`).
# 3. Runs a batch S&P 500 evaluation (`python main.py --batch 10`).
# ==============================================================================
def main() -> None:
    load_dotenv()
    configure_logging()

    parser = argparse.ArgumentParser(description="DAGER — Investment Research & Decision Engine")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Web server host (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8000, help="Web server port (default: 8000).")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload for development.")
    parser.add_argument("--ticker", type=str, help="Run a single company evaluation in the terminal and exit.")
    parser.add_argument("--batch", type=int, help="Run a batch S&P 500 evaluation for N companies and exit.")
    args = parser.parse_args()

    if args.ticker:
        from flows.evaluation_flow import evaluate_single_company_flow

        rep = evaluate_single_company_flow(args.ticker)
        print(f"\n=== {rep.company_name} ({rep.ticker}) ===")
        print(f"Headline (6m-3y): {rep.verdict.value} | Score: {rep.composite_score}/100")
        for h_key in ("1m", "3m", "6m", "1y", "3y", "5y"):
            sig = rep.horizon_signals.get(h_key)
            if sig:
                print(f"  [{h_key}] {sig.verdict.value:<10} ({sig.score:.1f}/100) - {sig.driver_summary}")
        print(f"\nSummary: {rep.thesis_summary}\n")
        return

    if args.batch:
        from flows.evaluation_flow import daily_sp500_evaluation_flow

        reports = daily_sp500_evaluation_flow(limit=args.batch)
        print(f"\nCompleted batch evaluation of {len(reports)} S&P 500 companies.")
        return

    print(f"Starting DAGER Web Server at http://{args.host}:{args.port} (DB: {settings.database_url.split('@')[-1]})")
    uvicorn.run("dager.web.api:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()

