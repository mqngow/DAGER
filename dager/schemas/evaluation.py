"""Final 6-month to 3-year stock evaluation & recommendation output schema."""

from datetime import datetime
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field


# ==============================================================================
# Investment Verdict & Horizon Enumerations
# Defines the discrete decision tiers and target holding horizon produced by DAGER.
# ==============================================================================
class InvestmentVerdict(str, Enum):
    STRONG_BUY = "Strong Buy"
    BUY = "Buy"
    HOLD = "Hold"
    AVOID = "Avoid"


class InvestmentHorizon(str, Enum):
    SIX_MONTHS_TO_THREE_YEARS = "6m-3y"


# ==============================================================================
# Pillar 1 Breakdown: Balance Sheet & Fundamental Health
# Summarizes key solvency, liquidity, cash flow, and profitability ratios
# computed from SEC EDGAR 10-K/10-Q filings.
# ==============================================================================
class FundamentalBreakdown(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    # --- Liquidity & Solvency (Balance Sheet) ---
    current_ratio: Optional[float] = Field(default=None, description="Current Assets / Current Liabilities.")
    quick_ratio: Optional[float] = Field(default=None, description="(Cash + Receivables) / Current Liabilities.")
    cash_to_short_term_debt: Optional[float] = Field(default=None, description="Cash / Short-Term Debt coverage.")
    debt_to_equity: Optional[float] = Field(default=None, description="Total Debt / Stockholders' Equity.")
    net_debt_to_ebitda: Optional[float] = Field(default=None, description="Net Debt / EBITDA leverage ratio.")
    book_value_growth_yoy: Optional[float] = Field(default=None, description="Year-over-year equity growth rate.")

    # --- Cash Generation & Profitability ---
    free_cash_flow_latest: Optional[float] = Field(default=None, description="Most recent annual/TTM Free Cash Flow (USD).")
    fcf_conversion_rate: Optional[float] = Field(default=None, description="Free Cash Flow / Net Income.")
    roic: Optional[float] = Field(default=None, description="Return on Invested Capital (NOPAT / Invested Capital).")
    operating_margin: Optional[float] = Field(default=None, description="Operating Income / Revenue.")
    net_margin: Optional[float] = Field(default=None, description="Net Income / Revenue.")
    revenue_cagr_3y: Optional[float] = Field(default=None, description="3-year compound annual revenue growth rate.")

    # --- Pillar Score (0–100) ---
    balance_sheet_score: float = Field(..., ge=0.0, le=100.0, description="Balance sheet liquidity & solvency sub-score.")
    profitability_score: float = Field(..., ge=0.0, le=100.0, description="Cash flow & ROIC profitability sub-score.")
    pillar_score: float = Field(..., ge=0.0, le=100.0, description="Combined Fundamental Health score (0–100).")


# ==============================================================================
# Pillar 2 Breakdown: Valuation & Price Action
# Compares intrinsic value estimates against current market price to quantify
# margin of safety and 6-month to 3-year upside potential.
# ==============================================================================
class ValuationBreakdown(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    current_price: float = Field(..., gt=0, description="Current stock price (USD).")
    estimated_fair_value: Optional[float] = Field(default=None, gt=0, description="Intrinsic fair value estimate per share (USD).")
    margin_of_safety_pct: Optional[float] = Field(
        default=None,
        description="Percentage discount of current price relative to fair value ((Fair - Price) / Fair).",
    )
    implied_upside_pct: Optional[float] = Field(
        default=None,
        description="Projected 6m–3y percentage upside/downside to fair value ((Fair - Price) / Price).",
    )
    pe_ratio: Optional[float] = Field(default=None, description="Price-to-Earnings multiple.")
    ev_to_ebitda: Optional[float] = Field(default=None, description="Enterprise Value-to-EBITDA multiple.")
    fcf_yield: Optional[float] = Field(default=None, description="Free Cash Flow / Market Cap yield.")
    pillar_score: float = Field(..., ge=0.0, le=100.0, description="Valuation & Price Action score (0–100).")


# ==============================================================================
# Final Stock Evaluation Report Schema
# The top-level contract stored in the database and returned by the API/UI for
# both pre-computed S&P 500 constituents and on-demand ticker lookups.
# ==============================================================================
class StockEvaluationReport(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    # --- Identity & Execution Metadata ---
    ticker: str = Field(..., description="Uppercase ticker symbol.")
    cik: str = Field(..., description="10-digit SEC CIK.")
    company_name: str = Field(..., description="Official company name.")
    is_sp500: bool = Field(default=False, description="Whether company is in the pre-computed S&P 500 store.")
    was_cached: bool = Field(default=False, description="True if served from fresh DB cache; False if computed on-demand.")
    evaluated_at: datetime = Field(default_factory=datetime.utcnow, description="UTC timestamp of evaluation.")
    horizon: InvestmentHorizon = Field(
        default=InvestmentHorizon.SIX_MONTHS_TO_THREE_YEARS,
        description="Target investment holding horizon (6m–3y).",
    )

    # --- Composite Verdict & Score ---
    composite_score: float = Field(
        ...,
        ge=0.0,
        le=100.0,
        description="Weighted multi-factor composite score (0–100).",
    )
    verdict: InvestmentVerdict = Field(..., description="Actionable recommendation tier.")

    # --- Three-Pillar Breakdowns ---
    fundamentals: FundamentalBreakdown = Field(..., description="Pillar 1: Balance sheet, cash flow & ROIC breakdown.")
    valuation: ValuationBreakdown = Field(..., description="Pillar 2: Price vs. intrinsic valuation breakdown.")
    sentiment_score: float = Field(
        ...,
        ge=0.0,
        le=100.0,
        description="Pillar 3: Recency-weighted FinBERT news & filing sentiment score (0–100).",
    )

    # --- Human-Readable Thesis ---
    bull_case: List[str] = Field(
        default_factory=list,
        description="Key balance sheet, valuation, and sentiment strengths supporting long-term upside.",
    )
    bear_case: List[str] = Field(
        default_factory=list,
        description="Key solvency, valuation, or qualitative risks over the 6m–3y horizon.",
    )
    thesis_summary: str = Field(
        ...,
        description="Concise executive summary explaining the 6-month to 3-year investment rating.",
    )

