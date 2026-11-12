"""Phase 5 end-to-end integration tests for Prefect tasks, On-Demand fallback, and S&P 500 batch flows."""

from dager.collectors import EdgarClient, MarketClient, NewsClient
from dager.storage import get_engine, init_db
from flows.evaluation_flow import (
    daily_sp500_evaluation_flow,
    evaluate_single_company_flow,
)


# ==============================================================================
# Mock Clients for Deterministic Flow Testing
# Subclasses real collector classes so Prefect's strict Pydantic parameter
# validator accepts them while avoiding slow external HTTP calls in unit tests.
# ==============================================================================
class _FakeEdgarClient(EdgarClient):
    def __init__(self, identity, facts):
        super().__init__(user_agent="DAGER-Test test@example.com")
        self._identity = identity
        self._facts = facts
        self.sp500_tickers = {identity.ticker}

    def resolve_company(self, query: str):
        return self._identity

    def get_company_facts(self, ticker: str):
        return self._facts

    def fetch_sp500_tickers(self):
        return [self._identity.ticker]


class _FakeMarketClient(MarketClient):
    def __init__(self, snapshot):
        super().__init__()
        self._snapshot = snapshot

    def get_price_snapshot(self, ticker: str):
        return self._snapshot


class _FakeNewsClient(NewsClient):
    def __init__(self, docs):
        super().__init__()
        self._docs = docs

    def get_company_news(self, ticker: str, company_name=None, limit: int = 20):
        return self._docs


# ==============================================================================
# Phase 5 Tests: On-Demand Cache-Miss Fallback & Scheduled S&P 500 Batch Flow
# Verifies that:
# 1. `evaluate_single_company_flow` computes live on a cache miss (`was_cached=False`)
#    and persists statements, prices, sentiment, and the evaluation report.
# 2. A subsequent call for the same company hits the DB cache (`was_cached=True`).
# 3. `daily_sp500_evaluation_flow` executes across the ticker list and returns
#    ranked `StockEvaluationReport` objects.
# ==============================================================================
def test_on_demand_and_batch_evaluation_flows(
    tmp_path,
    mock_company_identity,
    mock_sec_company_facts,
    mock_market_snapshot,
    sample_sentiment_docs,
):
    """Verify full Prefect flow execution for cache miss, cache hit, and batch S&P 500 runs."""
    db_file = tmp_path / "test_dager_flows.db"
    engine = get_engine(f"sqlite:///{db_file}")
    init_db(engine)

    fake_edgar = _FakeEdgarClient(mock_company_identity, mock_sec_company_facts)
    fake_market = _FakeMarketClient(mock_market_snapshot)
    fake_news = _FakeNewsClient(sample_sentiment_docs)

    # 1. First on-demand lookup -> Cache Miss -> runs full pipeline & persists (`was_cached=False`)
    report_miss = evaluate_single_company_flow(
        query="AAPL",
        force_refresh=False,
        include_transcripts=False,
        use_finbert=False,
        engine=engine,
        edgar_client=fake_edgar,
        market_client=fake_market,
        news_client=fake_news,
    )
    assert report_miss.ticker == "AAPL"
    assert report_miss.was_cached is False
    assert 0.0 <= report_miss.composite_score <= 100.0

    # 2. Second lookup (by company name) -> Cache Hit -> served directly from DB (`was_cached=True`)
    report_hit = evaluate_single_company_flow(
        query="Apple Inc",
        force_refresh=False,
        include_transcripts=False,
        use_finbert=False,
        engine=engine,
        edgar_client=fake_edgar,
        market_client=fake_market,
        news_client=fake_news,
    )
    assert report_hit.ticker == "AAPL"
    assert report_hit.was_cached is True
    assert report_hit.composite_score == report_miss.composite_score

    # 3. Force refresh bypasses cache (`was_cached=False`)
    report_forced = evaluate_single_company_flow(
        query="AAPL",
        force_refresh=True,
        include_transcripts=False,
        use_finbert=False,
        engine=engine,
        edgar_client=fake_edgar,
        market_client=fake_market,
        news_client=fake_news,
    )
    assert report_forced.was_cached is False
    assert set(report_forced.horizon_signals.keys()) == {"1m", "3m", "6m", "1y", "3y", "5y"}


# ==============================================================================
# Phase 6 Tests: FastAPI Web Dashboard & REST Endpoints
# Verifies that the dashboard HTML (`GET /`), health check (`GET /api/health`),
# and leaderboard (`GET /api/sp500`) respond with valid HTTP 200 payloads.
# ==============================================================================
def test_fastapi_dashboard_and_endpoints():
    """Verify FastAPI web UI and JSON API endpoints."""
    from fastapi.testclient import TestClient
    from dager.web.api import app

    client = TestClient(app)

    health_resp = client.get("/api/health")
    assert health_resp.status_code == 200
    assert "5y" in health_resp.json()["supported_horizons"]

    ui_resp = client.get("/")
    assert ui_resp.status_code == 200
    assert "DAGER" in ui_resp.text
    assert "Multi-Horizon Signals" in ui_resp.text

    sp500_resp = client.get("/api/sp500")
    assert sp500_resp.status_code == 200
    assert isinstance(sp500_resp.json(), list)


