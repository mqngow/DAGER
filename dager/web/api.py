"""FastAPI backend for ticker/company search, S&P 500 leaderboard, and on-demand evaluation."""

import base64
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import threading
from typing import Any, Dict, List, Optional
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
import httpx
from pydantic import BaseModel
from sqlalchemy.orm import Session

from dager.config import configure_logging, settings
from dager.schemas.evaluation import StockEvaluationReport
from dager.storage import (
    EvaluationRepo,
    User,
    UserRepo,
    get_session_factory,
    init_db,
    session_scope,
)
from flows.evaluation_flow import (
    daily_sp500_evaluation_flow,
    evaluate_single_company_flow,
)

# Ensure `logs/dager.log` + console handlers are attached even when started directly via Uvicorn
configure_logging()
logger = logging.getLogger("dager.web")

UI_HTML_PATH = Path(__file__).parent / "ui" / "index.html"

# ==============================================================================
# Continuous Background S&P 500 Sync Worker State
# Tracks live progress and keeps caching S&P 500 tickers daily until caught up.
# ==============================================================================
_SYNC_LOCK = threading.Lock()
_SYNC_WAKE_EVENT = threading.Event()
_SYNC_THREAD: Optional[threading.Thread] = None
_SYNC_TARGET_LIMIT: Optional[int] = None
_SYNC_STATE: Dict[str, Any] = {
    "is_running": False,
    "cached_count": 0,
    "total_sp500": 503,
    "current_ticker": None,
    "last_error": None,
}


def _sp500_sync_worker() -> None:
    """Daemon loop that continuously resumes S&P 500 caching until full daily coverage is reached."""
    while True:
        _SYNC_WAKE_EVENT.clear()
        with _SYNC_LOCK:
            target_limit = _SYNC_TARGET_LIMIT
            _SYNC_STATE["is_running"] = True
            _SYNC_STATE["last_error"] = None

        try:
            with session_scope() as session:
                before_count = len(EvaluationRepo(session).get_fresh_tickers())
        except Exception:
            before_count = 0

        def _on_progress(symbol: str, completed_count: int, total_count: int) -> None:
            with _SYNC_LOCK:
                _SYNC_STATE["current_ticker"] = symbol
                _SYNC_STATE["cached_count"] = completed_count
                _SYNC_STATE["total_sp500"] = total_count

        try:
            daily_sp500_evaluation_flow(
                limit=target_limit,
                force_refresh=False,
                include_transcripts=False,
                max_passes=2,
                progress_callback=_on_progress,
            )
        except Exception as exc:
            logger.exception("Background S&P 500 daily sync cycle failed.")
            with _SYNC_LOCK:
                _SYNC_STATE["last_error"] = str(exc)

        try:
            with session_scope() as session:
                after_count = len(EvaluationRepo(session).get_fresh_tickers())
        except Exception:
            after_count = before_count

        with _SYNC_LOCK:
            _SYNC_STATE["is_running"] = False
            _SYNC_STATE["current_ticker"] = None
            _SYNC_STATE["cached_count"] = after_count
            expected_total = _SYNC_STATE["total_sp500"]

        # Choose next wait interval: quick retry if still incomplete and making progress,
        # moderate backoff if stalled on rate limits/errors, or 1h check once fully cached.
        if target_limit is not None or after_count >= expected_total:
            sleep_seconds = 3600
        elif after_count > before_count:
            sleep_seconds = 60
        else:
            sleep_seconds = 900

        logger.info(
            "Background S&P 500 sync cycle paused (%d/%d fresh). Next check in %ds.",
            after_count,
            expected_total,
            sleep_seconds,
        )
        _SYNC_WAKE_EVENT.wait(timeout=sleep_seconds)


def _start_background_sp500_sync(limit: Optional[int] = None) -> None:
    """Start or wake the daemon background worker to cache up to `limit` (or all) S&P 500 tickers."""
    global _SYNC_THREAD, _SYNC_TARGET_LIMIT
    normalized_limit = None if (limit is None or limit >= 503) else limit
    with _SYNC_LOCK:
        _SYNC_TARGET_LIMIT = normalized_limit
        if _SYNC_THREAD is None or not _SYNC_THREAD.is_alive():
            _SYNC_THREAD = threading.Thread(
                target=_sp500_sync_worker,
                name="dager-sp500-sync",
                daemon=True,
            )
            _SYNC_THREAD.start()
        _SYNC_WAKE_EVENT.set()


# ==============================================================================
# FastAPI Application & Database Session Dependency
# Initializes SQLite/PostgreSQL tables on startup and yields a scoped SQLAlchemy
# session per HTTP request.
# ==============================================================================
@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Ensure DB tables exist and launch continuous daily S&P 500 caching on server boot."""
    logger.info("DAGER FastAPI server starting up; initializing database tables.")
    init_db()
    if not os.getenv("PYTEST_CURRENT_TEST"):
        _start_background_sp500_sync()
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
# Authentication & Session Token Helpers
# Signs and verifies stateless HMAC-SHA256 Bearer tokens for Google-signed-in
# users, keeping per-user session state lightweight.
# ==============================================================================
def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(val: str) -> bytes:
    pad = "=" * (-len(val) % 4)
    return base64.urlsafe_b64decode(val + pad)


def _create_auth_token(user: User) -> str:
    """Create a signed session token encoding the user's ID, Google sub, and email."""
    payload = {"uid": user.id, "sub": user.google_sub, "email": user.email}
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(
        settings.auth_secret_key.encode("utf-8"),
        payload_b64.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{payload_b64}.{_b64url_encode(sig)}"


def _verify_auth_token(token: str, session: Session) -> Optional[User]:
    """Validate an HMAC session token and return the corresponding `User`, or `None`."""
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        expected_sig = hmac.new(
            settings.auth_secret_key.encode("utf-8"),
            payload_b64.encode("ascii"),
            hashlib.sha256,
        ).digest()
        provided_sig = _b64url_decode(sig_b64)
        if not hmac.compare_digest(expected_sig, provided_sig):
            return None
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        user_id = int(payload["uid"])
    except Exception:
        return None
    return UserRepo(session).get_user_by_id(user_id)


def get_optional_user(
    authorization: Optional[str] = Header(default=None),
    session: Session = Depends(get_db_session),
) -> Optional[User]:
    """FastAPI dependency returning the signed-in `User` from `Authorization: Bearer ...`, or `None`."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        return None
    return _verify_auth_token(token, session)


def get_current_user(user: Optional[User] = Depends(get_optional_user)) -> User:
    """FastAPI dependency enforcing an authenticated user session."""
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in with Google required.")
    return user


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
    limit: int = Query(default=50, ge=1, le=505, description="Max companies to return."),
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
    user: Optional[User] = Depends(get_optional_user),
    session: Session = Depends(get_db_session),
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
        if user is not None:
            UserRepo(session).record_search(user.id, rep.ticker)
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
# 5. S&P 500 Sync Status & Batch Seed Endpoints
# `GET /api/sp500/sync-status` reports live background S&P 500 caching progress.
# `POST /api/sp500/seed` triggers either a quick synchronous sample (limit <= 10)
# or wakes the background worker for full S&P 500 caching (`background=True`).
# ==============================================================================
@app.get("/api/sp500/sync-status")
def get_sp500_sync_status(session: Session = Depends(get_db_session)) -> Dict[str, Any]:
    """Return live progress for the background S&P 500 daily caching worker."""
    fresh_count = len(EvaluationRepo(session).get_fresh_tickers())
    with _SYNC_LOCK:
        _SYNC_STATE["cached_count"] = fresh_count
        return dict(_SYNC_STATE)


@app.post("/api/sp500/seed", response_model=List[StockEvaluationReport])
def seed_sp500_sample(
    limit: int = Query(default=5, ge=1, le=503, description="Number of S&P 500 companies to evaluate."),
    background: bool = Query(default=False, description="Run batch sync asynchronously in the background worker."),
    session: Session = Depends(get_db_session),
) -> List[StockEvaluationReport]:
    """Run S&P 500 evaluation synchronously for small samples or wake the background sync worker."""
    logger.info("Web API batch seed requested (limit=%d, background=%s)", limit, background)
    if background or limit > 10:
        _start_background_sp500_sync(limit=limit)
        return EvaluationRepo(session).list_top_evaluations(sp500_only=True, limit=505)

    sample_tickers = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "BRK-B", "LLY", "AVGO", "JPM"][:limit]
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


# ==============================================================================
# 6. Google Sign-In, Per-User Search History & Favorites Endpoints
# Allows optional Google account authentication (`POST /api/auth/google`),
# returns per-user search history with automatic refresh after 1h cache expiry
# (`GET /api/user/recent`), and manages favorite tickers (`GET/POST /api/user/favorites`).
# ==============================================================================
class GoogleLoginRequest(BaseModel):
    credential: Optional[str] = None
    email: Optional[str] = None
    name: Optional[str] = None
    picture_url: Optional[str] = None


def _serialize_user_payload(user: User, favorites: List[str]) -> Dict[str, Any]:
    return {
        "user": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "picture_url": user.picture_url,
        },
        "favorites": favorites,
    }


@app.get("/api/auth/config")
def get_auth_config() -> Dict[str, Any]:
    """Return Google OAuth Client ID configuration for the dashboard Sign-In widget."""
    return {
        "google_client_id": settings.google_client_id,
        "google_oauth_enabled": bool(settings.google_client_id),
    }


@app.post("/api/auth/google")
def login_with_google(
    req: GoogleLoginRequest,
    session: Session = Depends(get_db_session),
) -> Dict[str, Any]:
    """Verify a Google ID token (or local Google email fallback) and issue a signed session token."""
    if req.credential:
        try:
            resp = httpx.get(
                "https://oauth2.googleapis.com/tokeninfo",
                params={"id_token": req.credential},
                timeout=10.0,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Google token verification failed: {exc}") from exc

        if resp.status_code != 200:
            raise HTTPException(status_code=401, detail="Invalid Google ID token.")
        token_info = resp.json()
        if settings.google_client_id and token_info.get("aud") != settings.google_client_id:
            raise HTTPException(status_code=401, detail="Google token audience mismatch.")

        google_sub = token_info.get("sub")
        email = token_info.get("email")
        if not google_sub or not email:
            raise HTTPException(status_code=401, detail="Google token missing subject or email.")
        name = token_info.get("name") or email.split("@")[0]
        picture_url = token_info.get("picture")
    elif req.email and req.email.strip():
        if settings.google_client_id:
            raise HTTPException(status_code=400, detail="Google credential token is required when OAuth is enabled.")
        email = req.email.strip().lower()
        google_sub = f"google:{email}"
        name = (req.name or "").strip() or email.split("@")[0]
        picture_url = req.picture_url
    else:
        raise HTTPException(status_code=400, detail="Provide a Google credential or email address.")

    user_repo = UserRepo(session)
    user = user_repo.upsert_user(
        google_sub=google_sub,
        email=email,
        name=name,
        picture_url=picture_url,
    )
    favorites = user_repo.get_favorite_tickers(user.id)
    payload = _serialize_user_payload(user, favorites)
    payload["token"] = _create_auth_token(user)
    return payload


@app.get("/api/auth/me")
def get_signed_in_profile(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
) -> Dict[str, Any]:
    """Return the signed-in user's profile and favorited ticker symbols."""
    favorites = UserRepo(session).get_favorite_tickers(user.id)
    return _serialize_user_payload(user, favorites)


def _resolve_user_ticker_reports(
    tickers: List[str],
    session: Session,
    refresh_stale: bool = True,
) -> List[StockEvaluationReport]:
    """Load fresh cached reports or re-evaluate stale (>1h) user tickers up to 3 per request."""
    eval_repo = EvaluationRepo(session)
    results: List[StockEvaluationReport] = []
    recomputed_count = 0

    for ticker in tickers:
        fresh = eval_repo.get_fresh_evaluation(ticker)
        if fresh is not None:
            results.append(fresh)
            continue

        if refresh_stale and recomputed_count < 3:
            try:
                recomputed = evaluate_single_company_flow(
                    query=ticker,
                    force_refresh=True,
                    include_transcripts=False,
                )
                recomputed_count += 1
                results.append(recomputed)
                continue
            except Exception:
                logger.exception("Failed refreshing expired user ticker '%s'", ticker)

        fallback = eval_repo.get_latest_evaluation(ticker)
        if fallback is not None:
            results.append(fallback)

    return results


@app.get("/api/user/recent", response_model=List[StockEvaluationReport])
def get_user_recent_tickers(
    limit: int = Query(default=50, ge=1, le=200, description="Max recent tickers for signed-in user."),
    refresh_stale: bool = Query(default=True, description="Automatically re-evaluate stale (>1h) tickers."),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
) -> List[StockEvaluationReport]:
    """Return evaluation reports for tickers saved in the signed-in user's search history."""
    rows = UserRepo(session).list_user_tickers(user.id, favorites_only=False, limit=limit)
    tickers = [r.ticker for r in rows]
    return _resolve_user_ticker_reports(tickers, session, refresh_stale=refresh_stale)


@app.get("/api/user/favorites", response_model=List[StockEvaluationReport])
def get_user_favorite_tickers(
    limit: int = Query(default=100, ge=1, le=200, description="Max favorited tickers to return."),
    refresh_stale: bool = Query(default=True, description="Automatically re-evaluate stale (>1h) favorites."),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
) -> List[StockEvaluationReport]:
    """Return evaluation reports for tickers favorited by the signed-in user."""
    rows = UserRepo(session).list_user_tickers(user.id, favorites_only=True, limit=limit)
    tickers = [r.ticker for r in rows]
    return _resolve_user_ticker_reports(tickers, session, refresh_stale=refresh_stale)


@app.post("/api/user/favorites/{ticker}")
def toggle_user_favorite_ticker(
    ticker: str,
    is_favorite: Optional[bool] = Query(
        default=None,
        description="Explicitly set favorite state (`true`/`false`), or omit to toggle.",
    ),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
) -> Dict[str, Any]:
    """Toggle or set favorite status for `ticker` on the signed-in user's account."""
    cleaned = ticker.strip().upper()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Ticker cannot be empty.")

    user_repo = UserRepo(session)
    row = user_repo.set_favorite(user.id, cleaned, is_favorite=is_favorite)
    favorites = user_repo.get_favorite_tickers(user.id)
    return {
        "ticker": row.ticker,
        "is_favorite": row.is_favorite,
        "favorites": favorites,
    }
