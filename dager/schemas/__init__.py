"""Data contracts & validation (Pydantic)."""

from dager.schemas.evaluation import (
    FundamentalBreakdown,
    HorizonSignal,
    InvestmentHorizon,
    InvestmentVerdict,
    StockEvaluationReport,
    ValuationBreakdown,
)
from dager.schemas.filings import (
    BalanceSheetPeriod,
    CashFlowPeriod,
    CompanyFinancials,
    CompanyIdentity,
    FilingForm,
    FiscalPeriod,
    IncomeStatementPeriod,
    MarketPriceSnapshot,
)
from dager.schemas.sentiment import (
    CompanySentimentSummary,
    SentimentDocument,
    SentimentLabel,
    SentimentSourceType,
)

__all__ = [
    # Filings & Market Schemas
    "FilingForm",
    "FiscalPeriod",
    "CompanyIdentity",
    "BalanceSheetPeriod",
    "IncomeStatementPeriod",
    "CashFlowPeriod",
    "MarketPriceSnapshot",
    "CompanyFinancials",
    # Sentiment Schemas
    "SentimentSourceType",
    "SentimentLabel",
    "SentimentDocument",
    "CompanySentimentSummary",
    # Evaluation Schemas
    "InvestmentVerdict",
    "InvestmentHorizon",
    "HorizonSignal",
    "FundamentalBreakdown",
    "ValuationBreakdown",
    "StockEvaluationReport",
]

