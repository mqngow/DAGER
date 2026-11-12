"""Prefect tasks for SEC filing downloads, XBRL parsing, market prices, and DB persistence."""

from typing import List, Optional
from sqlalchemy.engine import Engine

from dager.collectors import EdgarClient, MarketClient
from dager.core.fundamentals import (
    compute_fundamental_breakdown,
    parse_company_financials,
)
from dager.pipeline import prefect_task
from dager.schemas.evaluation import FundamentalBreakdown
from dager.schemas.filings import CompanyFinancials, CompanyIdentity
from dager.storage import FilingRepo, init_db, session_scope


# ==============================================================================
# 1. Company Resolution & S&P 500 Universe Tasks
# Resolves a user ticker/name query into a `CompanyIdentity` or fetches the
# full S&P 500 ticker list for scheduled daily batch runs.
# ==============================================================================
@prefect_task(name="resolve-company-identity", retries=2, retry_delay_seconds=2)
def resolve_company_task(
    query: str,
    edgar_client: Optional[EdgarClient] = None,
) -> CompanyIdentity:
    """Resolve a ticker symbol or fuzzy company name into a validated `CompanyIdentity`."""
    client = edgar_client or EdgarClient()
    return client.resolve_company(query)


@prefect_task(name="fetch-sp500-universe", retries=2, retry_delay_seconds=5)
def fetch_sp500_universe_task(
    limit: Optional[int] = None,
    edgar_client: Optional[EdgarClient] = None,
) -> List[str]:
    """Fetch the current list of S&P 500 constituent tickers (optionally capped by `limit`)."""
    client = edgar_client or EdgarClient()
    tickers = client.fetch_sp500_tickers()
    return tickers[:limit] if limit else tickers


# ==============================================================================
# 2. SEC EDGAR XBRL & Market Price Collection Task
# Pulls raw `companyfacts` JSON from SEC EDGAR and 5-year price history / market
# cap from `MarketClient`, normalizing both into a `CompanyFinancials` bundle.
# ==============================================================================
@prefect_task(name="collect-and-parse-financials", retries=2, retry_delay_seconds=3)
def collect_and_parse_financials_task(
    company: CompanyIdentity,
    form_filter: str = "10-K",
    max_periods: int = 5,
    edgar_client: Optional[EdgarClient] = None,
    market_client: Optional[MarketClient] = None,
) -> CompanyFinancials:
    """Fetch SEC XBRL facts and market price snapshot, returning normalized `CompanyFinancials`."""
    edgar = edgar_client or EdgarClient()
    market = market_client or MarketClient()

    raw_facts = edgar.get_company_facts(company.ticker)
    price_snapshot = market.get_price_snapshot(company.ticker)

    return parse_company_financials(
        company=company,
        company_facts=raw_facts,
        market_snapshot=price_snapshot,
        form_filter=form_filter,
        max_periods=max_periods,
    )


# ==============================================================================
# 3. Fundamental Ratios & Persistence Tasks
# Computes Pillar 1 (`FundamentalBreakdown`) and persists normalized statements
# and price snapshots into the relational database.
# ==============================================================================
@prefect_task(name="compute-fundamental-breakdown")
def compute_fundamentals_task(financials: CompanyFinancials) -> FundamentalBreakdown:
    """Compute balance sheet health, cash flow metrics, ROIC, and Pillar 1 score."""
    return compute_fundamental_breakdown(financials)


@prefect_task(name="persist-company-financials")
def persist_financials_task(
    financials: CompanyFinancials,
    engine: Optional[Engine] = None,
) -> int:
    """Save `CompanyFinancials` to relational storage and return the database `company.id`."""
    init_db(engine)
    with session_scope(engine) as session:
        repo = FilingRepo(session)
        company_row = repo.save_financials(financials)
        return company_row.id

