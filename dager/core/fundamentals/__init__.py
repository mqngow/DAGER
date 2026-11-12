"""Financial statement processing and SEC XBRL normalization."""

from dager.core.fundamentals.balance_sheet import (
    compute_balance_sheet_metrics,
    parse_balance_sheets,
)
from dager.core.fundamentals.cash_flow import (
    compute_cash_flow_metrics,
    parse_cash_flows,
)
from dager.core.fundamentals.income import (
    compute_income_metrics,
    parse_income_statements,
)
from dager.core.fundamentals.ratios import (
    compute_fundamental_breakdown,
    compute_roic,
    parse_company_financials,
)

__all__ = [
    "parse_balance_sheets",
    "compute_balance_sheet_metrics",
    "parse_income_statements",
    "compute_income_metrics",
    "parse_cash_flows",
    "compute_cash_flow_metrics",
    "parse_company_financials",
    "compute_roic",
    "compute_fundamental_breakdown",
]

