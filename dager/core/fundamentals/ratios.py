"""ROIC, Net Debt/EBITDA, cross-statement synthesis, and Pillar 1 Fundamental Breakdown."""

from typing import Any, Dict, Optional
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
from dager.schemas.evaluation import FundamentalBreakdown
from dager.schemas.filings import (
    CompanyFinancials,
    CompanyIdentity,
    MarketPriceSnapshot,
)


# ==============================================================================
# End-to-End SEC XBRL `companyfacts` Bundle Parser
# Converts raw SEC `companyfacts` JSON + CompanyIdentity + optional MarketPriceSnapshot
# into a single validated `CompanyFinancials` contract.
# ==============================================================================
def parse_company_financials(
    company: CompanyIdentity,
    company_facts: Dict[str, Any],
    market_snapshot: Optional[MarketPriceSnapshot] = None,
    form_filter: str = "10-K",
    max_periods: int = 5,
) -> CompanyFinancials:
    """Build a normalized `CompanyFinancials` bundle from raw SEC EDGAR `companyfacts` JSON."""
    balance_sheets = parse_balance_sheets(company_facts, form_filter=form_filter, max_periods=max_periods)
    income_statements = parse_income_statements(company_facts, form_filter=form_filter, max_periods=max_periods)
    cash_flows = parse_cash_flows(company_facts, form_filter=form_filter, max_periods=max_periods)

    return CompanyFinancials(
        company=company,
        balance_sheets=balance_sheets,
        income_statements=income_statements,
        cash_flows=cash_flows,
        market_snapshot=market_snapshot,
    )


# ==============================================================================
# Cross-Statement Ratios (ROIC & Net Debt / EBITDA) & Pillar 1 Scorer
# Combines Balance Sheet, Income Statement, and Cash Flow metrics into a
# comprehensive `FundamentalBreakdown` with a 0–100 Pillar 1 score.
# ==============================================================================
def compute_roic(financials: CompanyFinancials) -> Optional[float]:
    """
    Calculate Return on Invested Capital (ROIC = NOPAT / Invested Capital).
    - NOPAT = Operating Income * (1 - Effective Tax Rate, defaulted to 21% statutory).
    - Invested Capital = Total Debt + Stockholders' Equity - Cash & Equivalents.
    """
    if not financials.balance_sheets or not financials.income_statements:
        return None

    bs = financials.balance_sheets[0]
    inc = financials.income_statements[0]

    # Estimate effective tax rate, clamped to [0%, 40%], defaulting to 21% US corporate rate
    tax_rate = 0.21
    if inc.income_tax_expense is not None and inc.operating_income > 0:
        implied_tax = inc.income_tax_expense / inc.operating_income
        tax_rate = max(0.0, min(0.40, implied_tax))

    nopat = inc.operating_income * (1.0 - tax_rate)
    invested_capital = bs.total_debt + bs.stockholders_equity - bs.cash_and_equivalents

    # Fallback if excess cash makes net invested capital <= 0: use Total Assets - Current Liabilities
    if invested_capital <= 0:
        invested_capital = bs.total_assets - bs.current_liabilities
    if invested_capital <= 0:
        return None

    return round(nopat / invested_capital, 4)


def compute_fundamental_breakdown(financials: CompanyFinancials) -> FundamentalBreakdown:
    """Synthesize balance sheet, income, and cash flow statements into a `FundamentalBreakdown`."""
    bs_metrics = compute_balance_sheet_metrics(financials.balance_sheets)
    inc_metrics = compute_income_metrics(financials.income_statements)

    latest_ni = financials.income_statements[0].net_income if financials.income_statements else None
    latest_rev = financials.income_statements[0].revenue if financials.income_statements else None
    latest_cash = financials.balance_sheets[0].cash_and_equivalents if financials.balance_sheets else None

    cf_metrics = compute_cash_flow_metrics(
        financials.cash_flows,
        latest_net_income=latest_ni,
        latest_revenue=latest_rev,
        cash_on_hand=latest_cash,
    )

    # Cross-statement leverage: Net Debt / EBITDA
    net_debt_to_ebitda: Optional[float] = None
    ebitda = inc_metrics.get("ebitda_latest")
    if financials.balance_sheets and ebitda and ebitda > 0:
        net_debt = financials.balance_sheets[0].net_debt
        net_debt_to_ebitda = round(net_debt / ebitda, 3)

    roic = compute_roic(financials)

    # Compute Profitability & Cash Generation Sub-Score (0–100)
    prof_score = 50.0
    if roic is not None:
        if roic >= 0.18:
            prof_score += 20.0
        elif roic >= 0.10:
            prof_score += 12.0
        elif roic < 0.04:
            prof_score -= 12.0

    op_margin = inc_metrics.get("operating_margin")
    if op_margin is not None:
        if op_margin >= 0.20:
            prof_score += 12.0
        elif op_margin >= 0.10:
            prof_score += 6.0
        elif op_margin < 0.0:
            prof_score -= 15.0

    fcf_latest = cf_metrics.get("free_cash_flow_latest")
    if fcf_latest is not None:
        if fcf_latest > 0:
            prof_score += 10.0
        else:
            prof_score -= 12.0

    fcf_conv = cf_metrics.get("fcf_conversion_rate")
    if fcf_conv is not None and fcf_conv >= 0.85:
        prof_score += 5.0

    rev_cagr = inc_metrics.get("revenue_cagr_3y")
    if rev_cagr is not None:
        if rev_cagr >= 0.08:
            prof_score += 5.0
        elif rev_cagr < 0.0:
            prof_score -= 5.0

    prof_score = round(max(0.0, min(100.0, prof_score)), 2)
    bs_score = float(bs_metrics["balance_sheet_score"] or 50.0)

    # Adjust balance sheet score for Net Debt / EBITDA coverage when available
    if net_debt_to_ebitda is not None:
        if net_debt_to_ebitda <= 1.5:
            bs_score = min(100.0, bs_score + 5.0)
        elif net_debt_to_ebitda > 3.5:
            bs_score = max(0.0, bs_score - 10.0)

    # Pillar 1 weights Balance Sheet Health at 55% and Profitability/FCF at 45%
    pillar_score = round(0.55 * bs_score + 0.45 * prof_score, 2)

    return FundamentalBreakdown(
        current_ratio=bs_metrics["current_ratio"],
        quick_ratio=bs_metrics["quick_ratio"],
        cash_to_short_term_debt=bs_metrics["cash_to_short_term_debt"],
        debt_to_equity=bs_metrics["debt_to_equity"],
        net_debt_to_ebitda=net_debt_to_ebitda,
        book_value_growth_yoy=bs_metrics["book_value_growth_yoy"],
        free_cash_flow_latest=fcf_latest,
        fcf_conversion_rate=fcf_conv,
        roic=roic,
        operating_margin=op_margin,
        net_margin=inc_metrics.get("net_margin"),
        revenue_cagr_3y=rev_cagr,
        balance_sheet_score=round(bs_score, 2),
        profitability_score=prof_score,
        pillar_score=pillar_score,
    )

