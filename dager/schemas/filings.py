"""Raw & normalized 10-K / 10-Q financial statement and market price schemas."""

from datetime import date, datetime
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field, model_validator


# ==============================================================================
# Filing & Period Enumerations
# Standardizes SEC report types and fiscal period identifiers across collectors
# and core fundamental calculators.
# ==============================================================================
class FilingForm(str, Enum):
    FORM_10K = "10-K"
    FORM_10Q = "10-Q"


class FiscalPeriod(str, Enum):
    FY = "FY"
    Q1 = "Q1"
    Q2 = "Q2"
    Q3 = "Q3"
    Q4 = "Q4"


# ==============================================================================
# Company Identity Schema
# Represents a resolved public company (S&P 500 constituent or on-demand lookup).
# ==============================================================================
class CompanyIdentity(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ticker: str = Field(..., description="Uppercase stock ticker symbol (e.g., AAPL).")
    cik: str = Field(..., pattern=r"^\d{10}$", description="10-digit zero-padded SEC CIK.")
    company_name: str = Field(..., description="Official SEC registrant title.")
    sector: Optional[str] = Field(default=None, description="GICS sector or SIC classification.")
    is_sp500: bool = Field(
        default=False,
        description="True if part of the daily pre-computed S&P 500 universe.",
    )


# ==============================================================================
# Balance Sheet Period Schema
# Captures point-in-time assets, liabilities, and equity from XBRL `us-gaap` tags.
# Serves as the primary foundation for liquidity, solvency, and working capital checks.
# ==============================================================================
class BalanceSheetPeriod(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    # --- Period Metadata ---
    period_end: date = Field(..., description="Balance sheet snapshot date.")
    fiscal_year: int = Field(..., description="Fiscal year of the filing.")
    fiscal_period: FiscalPeriod = Field(..., description="Fiscal period (FY, Q1-Q4).")
    form: FilingForm = Field(..., description="Source SEC form (10-K or 10-Q).")
    filed_date: Optional[date] = Field(default=None, description="Date filed with the SEC.")

    # --- Assets (USD) ---
    cash_and_equivalents: float = Field(default=0.0, description="Cash, cash equivalents & short-term investments.")
    accounts_receivable: Optional[float] = Field(default=None, description="Net current accounts receivable.")
    inventory: Optional[float] = Field(default=None, description="Net inventory.")
    current_assets: float = Field(..., description="Total current assets.")
    total_assets: float = Field(..., description="Total assets.")

    # --- Liabilities & Equity (USD) ---
    accounts_payable: Optional[float] = Field(default=None, description="Current accounts payable.")
    short_term_debt: float = Field(default=0.0, description="Current portion of long-term debt & short-term borrowings.")
    current_liabilities: float = Field(..., description="Total current liabilities.")
    long_term_debt: float = Field(default=0.0, description="Non-current long-term debt.")
    total_liabilities: float = Field(..., description="Total liabilities.")
    stockholders_equity: float = Field(..., description="Total stockholders' equity (book value).")
    shares_outstanding: Optional[float] = Field(default=None, gt=0, description="Diluted or basic common shares outstanding.")

    # --- Derived Balance Sheet Helpers ---
    @property
    def total_debt(self) -> float:
        """Total short-term plus long-term debt obligations."""
        return self.short_term_debt + self.long_term_debt

    @property
    def net_debt(self) -> float:
        """Total debt minus cash and short-term equivalents."""
        return self.total_debt - self.cash_and_equivalents

    @property
    def working_capital(self) -> float:
        """Net working capital available for day-to-day operations."""
        return self.current_assets - self.current_liabilities


# ==============================================================================
# Income Statement Period Schema
# Captures top-line revenue, operating profitability, and net earnings for margin
# stability and growth trend analysis.
# ==============================================================================
class IncomeStatementPeriod(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    period_end: date = Field(..., description="Period end date.")
    fiscal_year: int = Field(..., description="Fiscal year.")
    fiscal_period: FiscalPeriod = Field(..., description="Fiscal period (FY, Q1-Q4).")
    form: FilingForm = Field(..., description="Source SEC form (10-K or 10-Q).")

    revenue: float = Field(..., description="Total top-line revenue / net sales.")
    gross_profit: Optional[float] = Field(default=None, description="Revenue minus cost of goods sold.")
    operating_income: float = Field(..., description="Operating income (EBIT).")
    interest_expense: Optional[float] = Field(default=None, description="Interest expense on debt.")
    income_tax_expense: Optional[float] = Field(default=None, description="Income tax provision.")
    net_income: float = Field(..., description="Bottom-line net income attributable to common shareholders.")
    ebitda: Optional[float] = Field(default=None, description="Earnings before interest, taxes, depreciation, and amortization.")
    eps_diluted: Optional[float] = Field(default=None, description="Diluted earnings per share.")


# ==============================================================================
# Cash Flow Statement Period Schema
# Captures operating cash flow and capital expenditures to compute Free Cash Flow
# (FCF), cash conversion efficiency, and operational runway.
# ==============================================================================
class CashFlowPeriod(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    period_end: date = Field(..., description="Period end date.")
    fiscal_year: int = Field(..., description="Fiscal year.")
    fiscal_period: FiscalPeriod = Field(..., description="Fiscal period (FY, Q1-Q4).")
    form: FilingForm = Field(..., description="Source SEC form (10-K or 10-Q).")

    operating_cash_flow: float = Field(..., description="Net cash provided by operating activities.")
    capital_expenditures: float = Field(
        default=0.0,
        ge=0.0,
        description="Capital expenditures (normalized as a non-negative outflow magnitude).",
    )
    depreciation_and_amortization: Optional[float] = Field(default=None, description="D&A expense.")
    dividends_paid: float = Field(default=0.0, ge=0.0, description="Cash dividends paid.")
    share_repurchases: float = Field(default=0.0, ge=0.0, description="Common stock repurchased.")

    @model_validator(mode="before")
    @classmethod
    def normalize_capex_sign(cls, data: dict) -> dict:
        """Ensure CapEx and cash outflows are stored as positive magnitudes even if raw XBRL reports negative numbers."""
        if isinstance(data, dict):
            for field_name in ("capital_expenditures", "dividends_paid", "share_repurchases"):
                val = data.get(field_name)
                if val is not None and val < 0:
                    data[field_name] = abs(val)
        return data

    @property
    def free_cash_flow(self) -> float:
        """Free Cash Flow = Operating Cash Flow - Capital Expenditures."""
        return self.operating_cash_flow - self.capital_expenditures


# ==============================================================================
# Market Price Snapshot Schema
# Pairs current market pricing and historical performance with SEC fundamentals
# so `valuation.py` can assess intrinsic value vs. market price.
# ==============================================================================
class MarketPriceSnapshot(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ticker: str = Field(..., description="Stock ticker symbol.")
    as_of: datetime = Field(default_factory=datetime.utcnow, description="Timestamp of price quote.")
    current_price: float = Field(..., gt=0, description="Latest share price in USD.")
    market_cap: Optional[float] = Field(default=None, gt=0, description="Total market capitalization in USD.")
    enterprise_value: Optional[float] = Field(default=None, description="Market cap plus net debt.")
    high_52w: Optional[float] = Field(default=None, gt=0, description="52-week high price.")
    low_52w: Optional[float] = Field(default=None, gt=0, description="52-week low price.")
    price_return_6m: Optional[float] = Field(default=None, description="Trailing 6-month price return (decimal, e.g., 0.12 = +12%).")
    price_return_1y: Optional[float] = Field(default=None, description="Trailing 1-year price return (decimal).")
    price_return_3y: Optional[float] = Field(default=None, description="Trailing 3-year price return (decimal).")


# ==============================================================================
# Normalized Company Financials Bundle
# Aggregates multi-period statements and market pricing for a single company into
# one clean contract passed from Collectors into Core Fundamental Calculators.
# ==============================================================================
class CompanyFinancials(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    company: CompanyIdentity
    balance_sheets: List[BalanceSheetPeriod] = Field(
        default_factory=list,
        description="Chronologically ordered balance sheet periods (most recent first).",
    )
    income_statements: List[IncomeStatementPeriod] = Field(
        default_factory=list,
        description="Chronologically ordered income statement periods (most recent first).",
    )
    cash_flows: List[CashFlowPeriod] = Field(
        default_factory=list,
        description="Chronologically ordered cash flow periods (most recent first).",
    )
    market_snapshot: Optional[MarketPriceSnapshot] = Field(
        default=None,
        description="Latest stock price and market valuation context.",
    )

