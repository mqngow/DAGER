"""Phase 1 & Phase 2 unit and live integration tests for Configuration, Schemas, and Collectors."""

from dager.collectors import EdgarClient, MarketClient, NewsClient
from dager.config import settings


# ==============================================================================
# Phase 1 Verification: Settings & `.env` Loading
# Validates that centralized settings load SEC User-Agent and API keys cleanly.
# ==============================================================================
def test_settings_loaded_from_env():
    """Verify `.env` configuration and SEC User-Agent are populated."""
    assert settings.sec_user_agent
    assert "@" in settings.sec_user_agent or len(settings.sec_user_agent) > 5
    assert settings.sec_min_interval_seconds >= 0.1


# ==============================================================================
# Phase 2 Verification: EdgarClient Ticker & Fuzzy Company Name Resolution
# Tests exact ticker matching, dot-to-hyphen normalization, and fuzzy title lookup.
# ==============================================================================
def test_edgar_company_resolver_unit():
    """Verify `resolve_company` resolves both ticker symbols and fuzzy company names."""
    client = EdgarClient()
    client.directory = {
        "AAPL": {"ticker": "AAPL", "cik": "0000320193", "title": "Apple Inc."},
        "MSFT": {"ticker": "MSFT", "cik": "0000789019", "title": "MICROSOFT CORP"},
        "BRK-B": {"ticker": "BRK-B", "cik": "0001067983", "title": "BERKSHIRE HATHAWAY INC"},
    }
    client.sp500_tickers = {"AAPL", "MSFT", "BRK-B"}

    # Exact ticker
    aapl = client.resolve_company("aapl")
    assert aapl.ticker == "AAPL"
    assert aapl.cik == "0000320193"
    assert aapl.is_sp500 is True

    # Dot-to-hyphen normalization
    brk = client.resolve_company("BRK.B")
    assert brk.ticker == "BRK-B"

    # Fuzzy company name resolution
    msft = client.resolve_company("Microsoft Corporation")
    assert msft.ticker == "MSFT"
    assert msft.cik == "0000789019"


# ==============================================================================
# Phase 2 Live Collector Smoke Tests
# Verifies live connectivity with SEC EDGAR, Yahoo Finance / Finnhub market
# pricing, and NewsClient (Finnhub / NewsAPI / RSS) using the user's `.env`.
# ==============================================================================
def test_live_edgar_and_market_and_news_collectors():
    """Verify live SEC CIK lookup, MarketClient snapshot, and NewsClient aggregation for AAPL."""
    edgar = EdgarClient()
    identity = edgar.resolve_company("AAPL")
    assert identity.ticker == "AAPL"
    assert identity.cik == "0000320193"

    market = MarketClient()
    snapshot = market.get_price_snapshot("AAPL")
    assert snapshot.ticker == "AAPL"
    assert snapshot.current_price > 0

    news = NewsClient()
    articles = news.get_company_news("AAPL", company_name="Apple", limit=5)
    assert isinstance(articles, list)
    assert len(articles) > 0
    assert articles[0].ticker == "AAPL"

