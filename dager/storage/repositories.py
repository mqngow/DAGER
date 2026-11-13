"""CRUD & cache-lookup repositories (`CompanyRepo`, `FilingRepo`, `EvaluationRepo`)."""

from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional
from sqlalchemy import desc, or_, select, update
from sqlalchemy.orm import Session

from dager.config import settings
from dager.schemas.evaluation import StockEvaluationReport
from dager.schemas.filings import (
    CompanyFinancials,
    CompanyIdentity,
    MarketPriceSnapshot,
)
from dager.schemas.sentiment import CompanySentimentSummary
from dager.storage.models import (
    Company,
    EvaluationResult,
    FinancialStatement,
    SentimentRecord,
    StockPrice,
    User,
    UserTicker,
)


# ==============================================================================
# 1. Company Repository (`CompanyRepo`)
# Handles upsert and lookup of `Company` records by ticker symbol, CIK, or
# case-insensitive company name substring.
# ==============================================================================
class CompanyRepo:
    def __init__(self, session: Session):
        self.session = session

    def upsert(self, identity: CompanyIdentity) -> Company:
        """Insert or update a `Company` row from a validated `CompanyIdentity` schema."""
        stmt = select(Company).where(
            or_(Company.ticker == identity.ticker.upper(), Company.cik == identity.cik)
        )
        company = self.session.execute(stmt).scalars().first()

        if company is None:
            company = Company(
                ticker=identity.ticker.upper(),
                cik=identity.cik,
                company_name=identity.company_name,
                sector=identity.sector,
                is_sp500=identity.is_sp500,
            )
            self.session.add(company)
        else:
            company.ticker = identity.ticker.upper()
            company.cik = identity.cik
            company.company_name = identity.company_name
            company.sector = identity.sector or company.sector
            company.is_sp500 = identity.is_sp500 or company.is_sp500
            company.updated_at = datetime.now(timezone.utc)

        self.session.flush()
        return company

    def find_by_query(self, query: str) -> Optional[Company]:
        """Locate a stored company by exact ticker, 10-digit CIK, or company name match."""
        q = query.strip()
        if not q:
            return None
        ticker_norm = q.upper().replace(".", "-")

        # 1. Exact ticker or CIK match
        stmt = select(Company).where(
            or_(Company.ticker == ticker_norm, Company.cik == q.zfill(10))
        )
        exact = self.session.execute(stmt).scalars().first()
        if exact is not None:
            return exact

        # 2. Case-insensitive company title substring match
        like_stmt = (
            select(Company)
            .where(Company.company_name.ilike(f"%{q}%"))
            .order_by(desc(Company.is_sp500))
        )
        return self.session.execute(like_stmt).scalars().first()


# ==============================================================================
# 2. Filing, Price & Sentiment Repository (`FilingRepo`)
# Persists multi-period financial statements, market price snapshots, and
# aggregated FinBERT sentiment summaries linked to a `Company`.
# ==============================================================================
class FilingRepo:
    def __init__(self, session: Session):
        self.session = session
        self.company_repo = CompanyRepo(session)

    def save_financials(self, financials: CompanyFinancials) -> Company:
        """Persist `CompanyFinancials` (balance sheets, income, cash flows, and price snapshot)."""
        company = self.company_repo.upsert(financials.company)

        # Index income and cash flow periods by `(period_end, form)` to pair with balance sheets
        inc_map = {(i.period_end, i.form.value): i for i in financials.income_statements}
        cf_map = {(c.period_end, c.form.value): c for c in financials.cash_flows}

        for bs in financials.balance_sheets:
            key = (bs.period_end, bs.form.value)
            inc = inc_map.get(key)
            cf = cf_map.get(key)

            stmt = select(FinancialStatement).where(
                FinancialStatement.company_id == company.id,
                FinancialStatement.period_end == bs.period_end,
                FinancialStatement.form == bs.form.value,
            )
            record = self.session.execute(stmt).scalars().first()

            payload = {
                "balance_sheet": bs.model_dump(mode="json"),
                "income_statement": inc.model_dump(mode="json") if inc else None,
                "cash_flow": cf.model_dump(mode="json") if cf else None,
            }

            if record is None:
                record = FinancialStatement(
                    company_id=company.id,
                    period_end=bs.period_end,
                    fiscal_year=bs.fiscal_year,
                    fiscal_period=bs.fiscal_period.value,
                    form=bs.form.value,
                )
                self.session.add(record)

            record.cash_and_equivalents = bs.cash_and_equivalents
            record.current_assets = bs.current_assets
            record.total_assets = bs.total_assets
            record.short_term_debt = bs.short_term_debt
            record.current_liabilities = bs.current_liabilities
            record.long_term_debt = bs.long_term_debt
            record.total_liabilities = bs.total_liabilities
            record.stockholders_equity = bs.stockholders_equity
            record.revenue = inc.revenue if inc else None
            record.operating_income = inc.operating_income if inc else None
            record.net_income = inc.net_income if inc else None
            record.free_cash_flow = cf.free_cash_flow if cf else None
            record.statement_json = payload

        if financials.market_snapshot is not None:
            self.save_price_snapshot(company.id, financials.market_snapshot)

        self.session.flush()
        return company

    def save_price_snapshot(self, company_id: int, snapshot: MarketPriceSnapshot) -> StockPrice:
        """Upsert the latest `StockPrice` snapshot row for a company to keep DB size bounded."""
        stmt = (
            select(StockPrice)
            .where(StockPrice.company_id == company_id)
            .order_by(desc(StockPrice.as_of), desc(StockPrice.id))
        )
        price_row = self.session.execute(stmt).scalars().first()
        if price_row is None:
            price_row = StockPrice(company_id=company_id, current_price=snapshot.current_price)
            self.session.add(price_row)

        price_row.as_of = snapshot.as_of
        price_row.current_price = snapshot.current_price
        price_row.market_cap = snapshot.market_cap
        price_row.enterprise_value = snapshot.enterprise_value
        price_row.high_52w = snapshot.high_52w
        price_row.low_52w = snapshot.low_52w
        price_row.price_return_6m = snapshot.price_return_6m
        price_row.price_return_1y = snapshot.price_return_1y
        price_row.price_return_3y = snapshot.price_return_3y
        self.session.flush()
        return price_row

    def save_sentiment(self, company_id: int, summary: CompanySentimentSummary) -> SentimentRecord:
        """Upsert the latest `SentimentRecord` row for a company to keep DB size bounded."""
        stmt = (
            select(SentimentRecord)
            .where(SentimentRecord.company_id == company_id)
            .order_by(desc(SentimentRecord.evaluated_at), desc(SentimentRecord.id))
        )
        record = self.session.execute(stmt).scalars().first()
        if record is None:
            record = SentimentRecord(company_id=company_id)
            self.session.add(record)

        record.evaluated_at = summary.evaluated_at
        record.document_count = summary.document_count
        record.weighted_polarity = summary.weighted_polarity
        record.sentiment_score_0_100 = summary.sentiment_score_0_100
        record.overall_label = summary.overall_label.value
        record.summary_json = summary.model_dump(mode="json")
        self.session.flush()
        return record


# ==============================================================================
# 3. Evaluation & Cache-Lookup Repository (`EvaluationRepo`)
# Implements the dual execution persistence pattern:
# - Stores daily S&P 500 and on-demand `StockEvaluationReport` objects.
# - Provides `get_fresh_evaluation()` and `get_or_evaluate()` to serve cached
#   reports when within TTL or trigger an on-demand evaluation callback on miss.
# ==============================================================================
class EvaluationRepo:
    def __init__(self, session: Session):
        self.session = session
        self.company_repo = CompanyRepo(session)

    def save_evaluation(self, report: StockEvaluationReport) -> EvaluationResult:
        """Upsert a `StockEvaluationReport` for its `Company` so stored DB size stays flat."""
        company = self.company_repo.upsert(
            CompanyIdentity(
                ticker=report.ticker,
                cik=report.cik,
                company_name=report.company_name,
                is_sp500=report.is_sp500,
            )
        )

        stmt = (
            select(EvaluationResult)
            .where(EvaluationResult.company_id == company.id)
            .order_by(desc(EvaluationResult.evaluated_at), desc(EvaluationResult.id))
        )
        row = self.session.execute(stmt).scalars().first()
        if row is None:
            row = EvaluationResult(company_id=company.id, ticker=company.ticker)
            self.session.add(row)

        row.ticker = company.ticker
        row.evaluated_at = report.evaluated_at
        row.horizon = report.horizon.value
        row.composite_score = report.composite_score
        row.verdict = report.verdict.value
        row.fundamentals_score = report.fundamentals.pillar_score
        row.balance_sheet_score = report.fundamentals.balance_sheet_score
        row.valuation_score = report.valuation.pillar_score
        row.sentiment_score = report.sentiment_score
        row.thesis_summary = report.thesis_summary
        row.report_json = report.model_dump(mode="json")
        self.session.flush()
        return row

    def get_fresh_evaluation(
        self,
        query: str,
        reference_time: Optional[datetime] = None,
    ) -> Optional[StockEvaluationReport]:
        """
        Look up the most recent `EvaluationResult` for `query` (ticker or company name).
        Returns a `StockEvaluationReport` with `was_cached=True` if still within TTL
        (`sp500_cache_ttl_hours` for S&P 500 companies, `ondemand_cache_ttl_hours` otherwise),
        or `None` on a cache miss / expired record.
        """
        company = self.company_repo.find_by_query(query)
        if company is None:
            return None

        stmt = (
            select(EvaluationResult)
            .where(EvaluationResult.company_id == company.id)
            .order_by(desc(EvaluationResult.evaluated_at), desc(EvaluationResult.id))
        )
        latest = self.session.execute(stmt).scalars().first()
        if latest is None:
            return None

        ttl_hours = (
            settings.sp500_cache_ttl_hours
            if company.is_sp500
            else settings.ondemand_cache_ttl_hours
        )
        now = reference_time or datetime.now(timezone.utc)
        eval_dt = (
            latest.evaluated_at.astimezone(timezone.utc)
            if latest.evaluated_at.tzinfo
            else latest.evaluated_at.replace(tzinfo=timezone.utc)
        )

        if (now - eval_dt) > timedelta(hours=ttl_hours):
            return None

        report = StockEvaluationReport.model_validate(latest.report_json)
        return report.model_copy(update={"was_cached": True})

    def get_or_evaluate(
        self,
        query: str,
        evaluator_fn: Callable[[str], StockEvaluationReport],
        force_refresh: bool = False,
    ) -> StockEvaluationReport:
        """
        Core Dual-Execution Cache Router:
        1. Checks relational storage for a fresh evaluation matching `query`.
        2. On cache hit (and `not force_refresh`), returns the stored report immediately (`was_cached=True`).
        3. On cache miss or stale data, invokes `evaluator_fn(query)` to run the live
           on-demand pipeline, persists the resulting report, and returns it (`was_cached=False`).
        """
        if not force_refresh:
            cached = self.get_fresh_evaluation(query)
            if cached is not None:
                return cached

        fresh_report = evaluator_fn(query).model_copy(update={"was_cached": False})
        self.save_evaluation(fresh_report)
        return fresh_report

    def list_top_evaluations(
        self,
        sp500_only: bool = True,
        limit: int = 50,
        sort_by: str = "score",
    ) -> List[StockEvaluationReport]:
        """Return latest evaluations sorted by `'score'` (leaderboard) or `'recent'` (search history)."""
        order_clauses = (
            (desc(EvaluationResult.evaluated_at), desc(EvaluationResult.id))
            if sort_by == "recent"
            else (desc(EvaluationResult.composite_score), desc(EvaluationResult.evaluated_at))
        )
        stmt = (
            select(EvaluationResult)
            .join(Company, EvaluationResult.company_id == Company.id)
            .order_by(*order_clauses)
        )
        if sp500_only:
            stmt = stmt.where(Company.is_sp500.is_(True))

        rows = self.session.execute(stmt).scalars().all()
        seen_tickers: set[str] = set()
        results: List[StockEvaluationReport] = []

        for row in rows:
            if row.ticker in seen_tickers:
                continue
            seen_tickers.add(row.ticker)
            results.append(
                StockEvaluationReport.model_validate(row.report_json).model_copy(
                    update={"was_cached": True}
                )
            )
            if len(results) >= limit:
                break

        return results

    def get_fresh_tickers(
        self,
        ttl_hours: Optional[int] = None,
        reference_time: Optional[datetime] = None,
        sp500_only: bool = True,
    ) -> set[str]:
        """Return tickers that already have a cached evaluation within the TTL window."""
        ttl = ttl_hours if ttl_hours is not None else settings.sp500_cache_ttl_hours
        now = reference_time or datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=ttl)

        stmt = select(EvaluationResult.ticker, EvaluationResult.evaluated_at)
        if sp500_only:
            stmt = stmt.join(Company, EvaluationResult.company_id == Company.id).where(
                Company.is_sp500.is_(True)
            )

        rows = self.session.execute(stmt).all()
        fresh: set[str] = set()
        for ticker, evaluated_at in rows:
            if evaluated_at is None:
                continue
            eval_dt = (
                evaluated_at.astimezone(timezone.utc)
                if evaluated_at.tzinfo
                else evaluated_at.replace(tzinfo=timezone.utc)
            )
            if eval_dt >= cutoff:
                fresh.add(ticker.upper())
        return fresh

    def ensure_sp500_flag(self, tickers: set[str]) -> None:
        """Ensure existing Company rows in `tickers` are marked `is_sp500=True`."""
        normalized = {t.strip().upper() for t in tickers if t and t.strip()}
        if not normalized:
            return
        self.session.execute(
            update(Company)
            .where(
                Company.ticker.in_(normalized),
                or_(Company.is_sp500.is_(False), Company.is_sp500.is_(None)),
            )
            .values(is_sp500=True)
        )
        self.session.flush()

    def get_latest_evaluation(self, query: str) -> Optional[StockEvaluationReport]:
        """Return the latest stored `StockEvaluationReport` for `query` regardless of TTL."""
        company = self.company_repo.find_by_query(query)
        if company is None:
            return None
        stmt = (
            select(EvaluationResult)
            .where(EvaluationResult.company_id == company.id)
            .order_by(desc(EvaluationResult.evaluated_at), desc(EvaluationResult.id))
        )
        latest = self.session.execute(stmt).scalars().first()
        if latest is None:
            return None
        return StockEvaluationReport.model_validate(latest.report_json).model_copy(
            update={"was_cached": True}
        )


# ==============================================================================
# 4. User Account & Personal Ticker History Repository (`UserRepo`)
# Manages Google-authenticated user profiles, per-user ticker search history,
# and favorited ticker tracking using tiny relational rows.
# ==============================================================================
class UserRepo:
    def __init__(self, session: Session):
        self.session = session

    def upsert_user(
        self,
        google_sub: str,
        email: str,
        name: str,
        picture_url: Optional[str] = None,
    ) -> User:
        """Create or update a Google-authenticated `User` profile."""
        stmt = select(User).where(User.google_sub == google_sub)
        user = self.session.execute(stmt).scalars().first()
        now = datetime.now(timezone.utc)

        if user is None:
            user = User(
                google_sub=google_sub,
                email=email.strip().lower(),
                name=name.strip() or email.split("@")[0],
                picture_url=picture_url,
                created_at=now,
                last_login_at=now,
            )
            self.session.add(user)
        else:
            user.email = email.strip().lower()
            user.name = name.strip() or user.name
            if picture_url:
                user.picture_url = picture_url
            user.last_login_at = now

        self.session.flush()
        return user

    def get_user_by_id(self, user_id: int) -> Optional[User]:
        """Look up a `User` by primary key."""
        stmt = select(User).where(User.id == user_id)
        return self.session.execute(stmt).scalars().first()

    def record_search(self, user_id: int, ticker: str) -> UserTicker:
        """Save or bump `searched_at` for a ticker in the user's personal history."""
        norm_ticker = ticker.strip().upper()
        now = datetime.now(timezone.utc)
        stmt = select(UserTicker).where(
            UserTicker.user_id == user_id,
            UserTicker.ticker == norm_ticker,
        )
        row = self.session.execute(stmt).scalars().first()
        if row is None:
            row = UserTicker(
                user_id=user_id,
                ticker=norm_ticker,
                is_favorite=False,
                searched_at=now,
            )
            self.session.add(row)
        else:
            row.searched_at = now

        self.session.flush()
        return row

    def set_favorite(
        self,
        user_id: int,
        ticker: str,
        is_favorite: Optional[bool] = None,
    ) -> UserTicker:
        """Toggle or explicitly set favorite state for `(user_id, ticker)`."""
        norm_ticker = ticker.strip().upper()
        now = datetime.now(timezone.utc)
        stmt = select(UserTicker).where(
            UserTicker.user_id == user_id,
            UserTicker.ticker == norm_ticker,
        )
        row = self.session.execute(stmt).scalars().first()
        if row is None:
            target_state = True if is_favorite is None else bool(is_favorite)
            row = UserTicker(
                user_id=user_id,
                ticker=norm_ticker,
                is_favorite=target_state,
                searched_at=now,
                favorited_at=now if target_state else None,
            )
            self.session.add(row)
        else:
            target_state = (not row.is_favorite) if is_favorite is None else bool(is_favorite)
            row.is_favorite = target_state
            row.favorited_at = now if target_state else None

        self.session.flush()
        return row

    def list_user_tickers(
        self,
        user_id: int,
        favorites_only: bool = False,
        limit: int = 50,
    ) -> List[UserTicker]:
        """Return a user's tracked tickers ordered by most recently favorited or searched."""
        stmt = select(UserTicker).where(UserTicker.user_id == user_id)
        if favorites_only:
            stmt = stmt.where(UserTicker.is_favorite.is_(True)).order_by(
                desc(UserTicker.favorited_at),
                desc(UserTicker.searched_at),
            )
        else:
            stmt = stmt.order_by(desc(UserTicker.searched_at))
        stmt = stmt.limit(limit)
        return list(self.session.execute(stmt).scalars().all())

    def get_favorite_tickers(self, user_id: int) -> List[str]:
        """Return a sorted list of ticker symbols favorited by `user_id`."""
        stmt = (
            select(UserTicker.ticker)
            .where(UserTicker.user_id == user_id, UserTicker.is_favorite.is_(True))
            .order_by(UserTicker.ticker)
        )
        return list(self.session.execute(stmt).scalars().all())

