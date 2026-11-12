"""Relational ORM models (`Company`, `FinancialStatement`, `StockPrice`, `SentimentRecord`, `EvaluationResult`)."""

from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional
from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from dager.storage.db import Base


def _utc_now() -> datetime:
    """Return timezone-aware UTC timestamp for ORM column defaults."""
    return datetime.now(timezone.utc)


# ==============================================================================
# 1. Company Master Table (`companies`)
# Stores canonical company identity (ticker, 10-digit SEC CIK, official title,
# and whether it belongs to the daily pre-computed S&P 500 universe).
# ==============================================================================
class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(16), unique=True, index=True, nullable=False)
    cik: Mapped[str] = mapped_column(String(10), unique=True, index=True, nullable=False)
    company_name: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    sector: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    is_sp500: Mapped[bool] = mapped_column(Boolean, default=False, index=True, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now, onupdate=_utc_now)

    # Relationships to child records
    financial_statements: Mapped[List["FinancialStatement"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    stock_prices: Mapped[List["StockPrice"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    sentiment_records: Mapped[List["SentimentRecord"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    evaluations: Mapped[List["EvaluationResult"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )


# ==============================================================================
# 2. Financial Statement Table (`financial_statements`)
# Persists normalized 10-K and 10-Q balance sheet, income statement, and cash
# flow periods per company and fiscal period end date.
# ==============================================================================
class FinancialStatement(Base):
    __tablename__ = "financial_statements"
    __table_args__ = (
        UniqueConstraint("company_id", "period_end", "form", name="uq_company_period_form"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    period_end: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    fiscal_year: Mapped[int] = mapped_column(Integer, nullable=False)
    fiscal_period: Mapped[str] = mapped_column(String(8), nullable=False)
    form: Mapped[str] = mapped_column(String(8), nullable=False)

    # Key indexed balance sheet & cash flow columns for fast SQL filtering
    cash_and_equivalents: Mapped[float] = mapped_column(Float, default=0.0)
    current_assets: Mapped[float] = mapped_column(Float, default=0.0)
    total_assets: Mapped[float] = mapped_column(Float, default=0.0)
    short_term_debt: Mapped[float] = mapped_column(Float, default=0.0)
    current_liabilities: Mapped[float] = mapped_column(Float, default=0.0)
    long_term_debt: Mapped[float] = mapped_column(Float, default=0.0)
    total_liabilities: Mapped[float] = mapped_column(Float, default=0.0)
    stockholders_equity: Mapped[float] = mapped_column(Float, default=0.0)
    revenue: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    operating_income: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    net_income: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    free_cash_flow: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Full serialized Pydantic period payloads (`balance_sheet`, `income`, `cash_flow`)
    statement_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)

    company: Mapped["Company"] = relationship(back_populates="financial_statements")


# ==============================================================================
# 3. Stock Price Snapshot Table (`stock_prices`)
# Stores market price snapshots, market cap, enterprise value, and 6m/1y/3y
# trailing returns for intrinsic valuation comparisons.
# ==============================================================================
class StockPrice(Base):
    __tablename__ = "stock_prices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now, index=True)
    current_price: Mapped[float] = mapped_column(Float, nullable=False)
    market_cap: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    enterprise_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    high_52w: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    low_52w: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    price_return_6m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    price_return_1y: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    price_return_3y: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    company: Mapped["Company"] = relationship(back_populates="stock_prices")


# ==============================================================================
# 4. Sentiment Record Table (`sentiment_records`)
# Persists aggregated FinBERT sentiment scores, weighted polarity, and top
# bullish/bearish headlines for a company.
# ==============================================================================
class SentimentRecord(Base):
    __tablename__ = "sentiment_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now, index=True)
    document_count: Mapped[int] = mapped_column(Integer, default=0)
    weighted_polarity: Mapped[float] = mapped_column(Float, default=0.0)
    sentiment_score_0_100: Mapped[float] = mapped_column(Float, default=50.0)
    overall_label: Mapped[str] = mapped_column(String(32), default="neutral")
    summary_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)

    company: Mapped["Company"] = relationship(back_populates="sentiment_records")


# ==============================================================================
# 5. Evaluation Result Table (`evaluation_results`)
# Stores the synthesized 6-month to 3-year `StockEvaluationReport` (composite
# score, verdict, pillar scores, thesis, and full JSON payload) for instant
# S&P 500 cache hits and historical tracking.
# ==============================================================================
class EvaluationResult(Base):
    __tablename__ = "evaluation_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now, index=True)
    horizon: Mapped[str] = mapped_column(String(16), default="6m-3y")

    # Top-level decision metrics indexed for fast leaderboard ranking
    composite_score: Mapped[float] = mapped_column(Float, index=True, nullable=False)
    verdict: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    fundamentals_score: Mapped[float] = mapped_column(Float, nullable=False)
    balance_sheet_score: Mapped[float] = mapped_column(Float, nullable=False)
    valuation_score: Mapped[float] = mapped_column(Float, nullable=False)
    sentiment_score: Mapped[float] = mapped_column(Float, nullable=False)

    thesis_summary: Mapped[str] = mapped_column(Text, nullable=False)
    report_json: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False)

    company: Mapped["Company"] = relationship(back_populates="evaluations")

