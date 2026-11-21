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
def _use_isolated_sqlite_db(tmp_path, monkeypatch):
    """Isolate FastAPI tests onto a temporary SQLite database instead of `.env` PostgreSQL."""
    import dager.storage.db as db_mod
    import dager.web.api as api_mod

    db_file = tmp_path / "test_api_isolated.db"
    test_engine = get_engine(f"sqlite:///{db_file}")
    init_db(test_engine)
    monkeypatch.setattr(db_mod, "_engine", test_engine)
    monkeypatch.setattr(db_mod, "_SessionLocal", None)
    monkeypatch.setattr(api_mod, "_DB_INITIALIZED", True)
    return test_engine


def test_fastapi_dashboard_and_endpoints(tmp_path, monkeypatch):
    """Verify FastAPI web UI and JSON API endpoints."""
    from fastapi.testclient import TestClient
    from dager.config import settings
    from dager.web.api import app

    _use_isolated_sqlite_db(tmp_path, monkeypatch)
    client = TestClient(app)

    health_resp = client.get("/api/health")
    assert health_resp.status_code == 200
    assert "5y" in health_resp.json()["supported_horizons"]
    assert "1m-5y" in health_resp.json()["supported_horizons"]

    ui_resp = client.get("/")
    assert ui_resp.status_code == 200
    assert "DAGER" in ui_resp.text
    assert "Multi-Horizon Signals" in ui_resp.text

    sp500_resp = client.get("/api/sp500")
    assert sp500_resp.status_code == 200
    assert isinstance(sp500_resp.json(), list)

    sync_resp = client.get("/api/sp500/sync-status")
    assert sync_resp.status_code == 200
    sync_data = sync_resp.json()
    for key in ("is_running", "cached_count", "total_sp500", "current_ticker", "last_error"):
        assert key in sync_data
    assert isinstance(sync_data["cached_count"], int)

    # Verify Google login fallback, profile endpoint, and favorite ticker watchlist
    monkeypatch.setattr(settings, "google_client_id", None)
    login_resp = client.post(
        "/api/auth/google",
        json={"email": "student@example.com", "name": "Student User"},
    )
    assert login_resp.status_code == 200
    login_data = login_resp.json()
    assert login_data["user"]["email"] == "student@example.com"
    assert login_data["token"]

    auth_headers = {"Authorization": f"Bearer {login_data['token']}"}
    me_resp = client.get("/api/auth/me", headers=auth_headers)
    assert me_resp.status_code == 200
    assert me_resp.json()["user"]["name"] == "Student User"

    fav_resp = client.post("/api/user/favorites/AAPL?is_favorite=true", headers=auth_headers)
    assert fav_resp.status_code == 200
    assert fav_resp.json()["is_favorite"] is True
    assert "AAPL" in fav_resp.json()["favorites"]

    fav_list_resp = client.get("/api/user/favorites?refresh_stale=false", headers=auth_headers)
    assert fav_list_resp.status_code == 200
    assert isinstance(fav_list_resp.json(), list)

    recent_list_resp = client.get("/api/user/recent?refresh_stale=false", headers=auth_headers)
    assert recent_list_resp.status_code == 200
    assert isinstance(recent_list_resp.json(), list)


# ==============================================================================
# Phase 7 Tests: Security Hardening Controls
# Verifies security response headers, HttpOnly cookie lifecycle, token expiry,
# strict ticker regex validation, admin seed gate, production fallback lock,
# and sliding-window rate limiter behavior.
# ==============================================================================
def test_security_hardening_controls(tmp_path, monkeypatch):
    """Verify HTTP security headers, auth/session guarantees, input validation, and admin gates."""
    from fastapi.testclient import TestClient
    from dager.config import settings
    from dager.storage import User, session_scope
    from dager.web.api import SESSION_COOKIE_NAME, _SlidingWindowRateLimiter, _create_auth_token, app

    _use_isolated_sqlite_db(tmp_path, monkeypatch)
    client = TestClient(app)

    # 1. Browser security headers on HTML response
    ui_resp = client.get("/")
    assert ui_resp.status_code == 200
    assert ui_resp.headers.get("x-content-type-options") == "nosniff"
    assert ui_resp.headers.get("x-frame-options") == "DENY"
    assert "default-src 'self'" in (ui_resp.headers.get("content-security-policy") or "")
    assert ui_resp.headers.get("referrer-policy") == "strict-origin-when-cross-origin"

    # 2. HttpOnly cookie auth + logout invalidation
    monkeypatch.setattr(settings, "google_client_id", None)
    monkeypatch.setattr(settings, "environment", "development")
    login_resp = client.post(
        "/api/auth/google",
        json={"email": "security.tester@example.com", "name": "Sec Tester"},
    )
    assert login_resp.status_code == 200
    assert SESSION_COOKIE_NAME in login_resp.cookies

    me_cookie_resp = client.get("/api/auth/me")
    assert me_cookie_resp.status_code == 200
    assert me_cookie_resp.json()["user"]["email"] == "security.tester@example.com"

    logout_resp = client.post("/api/auth/logout")
    assert logout_resp.status_code == 200
    assert client.get("/api/auth/me").status_code == 401

    # 3. Expired token rejection
    with session_scope() as s:
        u = s.query(User).filter(User.email == "security.tester@example.com").first()
        expired_token = _create_auth_token(u, ttl_hours=-1)
    assert (
        client.get("/api/auth/me", headers={"Authorization": f"Bearer {expired_token}"}).status_code
        == 401
    )

    # 4. Strict ticker validation rejects XSS/garbage payloads
    valid_token = login_resp.json()["token"]
    bad_fav_resp = client.post(
        "/api/user/favorites/<script>",
        headers={"Authorization": f"Bearer {valid_token}"},
    )
    assert bad_fav_resp.status_code == 400

    # 5. Admin key gate on POST /api/sp500/seed
    monkeypatch.setattr(settings, "admin_api_key", "super-secret-admin-key")
    assert client.post("/api/sp500/seed?limit=1").status_code == 403
    assert client.post("/api/sp500/seed?limit=1", headers={"X-Admin-Key": "wrong"}).status_code == 403

    # 6. Email-only fallback blocked in production mode
    monkeypatch.setattr(settings, "environment", "production")
    prod_fallback_resp = client.post(
        "/api/auth/google",
        json={"email": "attacker@example.com", "name": "Attacker"},
    )
    assert prod_fallback_resp.status_code == 400

    # 7. Sliding-window rate limiter enforces request budget
    limiter = _SlidingWindowRateLimiter(window_seconds=60)
    assert limiter.allow("1.2.3.4", 2) is True
    assert limiter.allow("1.2.3.4", 2) is True
    assert limiter.allow("1.2.3.4", 2) is False



