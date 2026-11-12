"""Prefect flow for SEC EDGAR XBRL & market price ingestion (`fundamentals_flow.py`)."""

from typing import List, Optional
from sqlalchemy.engine import Engine

from dager.collectors import EdgarClient, MarketClient
from dager.pipeline import (
    collect_and_parse_financials_task,
    fetch_sp500_universe_task,
    persist_financials_task,
    prefect_flow,
    resolve_company_task,
)
from dager.schemas.filings import CompanyFinancials


# ==============================================================================
# 1. Single-Company Fundamentals Ingestion Flow
# Resolves a ticker or company name, downloads SEC 10-K XBRL facts + 5-year
# price snapshot, parses into `CompanyFinancials`, and stores in DB.
# ==============================================================================
@prefect_flow(name="ingest-company-fundamentals")
def ingest_company_fundamentals_flow(
    query: str,
    persist: bool = True,
    engine: Optional[Engine] = None,
    edgar_client: Optional[EdgarClient] = None,
    market_client: Optional[MarketClient] = None,
) -> CompanyFinancials:
    """Pull SEC EDGAR statements and market pricing for a single company and optionally persist."""
    identity = resolve_company_task(query, edgar_client=edgar_client)
    financials = collect_and_parse_financials_task(
        company=identity,
        edgar_client=edgar_client,
        market_client=market_client,
    )
    if persist:
        persist_financials_task(financials, engine=engine)
    return financials


# ==============================================================================
# 2. Batch S&P 500 Fundamentals Ingestion Flow
# Iterates across the S&P 500 constituent list (or a provided subset) and
# refreshes balance sheets, income statements, cash flows, and stock prices.
# ==============================================================================
@prefect_flow(name="batch-sp500-fundamentals")
def batch_sp500_fundamentals_flow(
    tickers: Optional[List[str]] = None,
    limit: Optional[int] = None,
    engine: Optional[Engine] = None,
) -> List[CompanyFinancials]:
    """Ingest and persist SEC XBRL fundamentals and market snapshots for S&P 500 constituents."""
    edgar = EdgarClient()
    market = MarketClient()
    universe = tickers or fetch_sp500_universe_task(limit=limit, edgar_client=edgar)

    results: List[CompanyFinancials] = []
    for symbol in universe:
        try:
            fin = ingest_company_fundamentals_flow(
                query=symbol,
                persist=True,
                engine=engine,
                edgar_client=edgar,
                market_client=market,
            )
            results.append(fin)
        except Exception:
            continue
    return results

