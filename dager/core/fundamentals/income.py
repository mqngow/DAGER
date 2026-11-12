"""Margins, revenue growth, EBITDA calculations, and SEC XBRL income statement extraction."""

from datetime import date
from typing import Any, Dict, List, Optional
from dager.schemas.filings import FilingForm, FiscalPeriod, IncomeStatementPeriod


# ==============================================================================
# SEC XBRL `us-gaap` Tag Dictionary for Income Statement Items
# Covers standard corporate, tech, retail, and financial revenue/profit concepts.
# ==============================================================================
INCOME_STATEMENT_TAGS: Dict[str, List[str]] = {
    "revenue": [
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "InterestAndDividendIncomeOperating",
    ],
    "gross_profit": [
        "GrossProfit",
    ],
    "operating_income": [
        "OperatingIncomeLoss",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    ],
    "interest_expense": [
        "InterestExpense",
        "InterestExpenseDebt",
    ],
    "income_tax_expense": [
        "IncomeTaxExpenseBenefit",
    ],
    "net_income": [
        "NetIncomeLoss",
        "ProfitLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
    ],
    "depreciation_amortization": [
        "DepreciationDepletionAndAmortization",
        "DepreciationAndAmortization",
    ],
    "eps_diluted": [
        "EarningsPerShareDiluted",
        "EarningsPerShareBasic",
    ],
}


# ==============================================================================
# XBRL Duration Fact Extractor (Full-Year & Quarterly Income Flows)
# Filters durational XBRL facts (approx. 365 days for 10-K, 90 days for 10-Q)
# so 3-month sub-periods inside a 10-K are not confused with full-year totals.
# ==============================================================================
def _extract_duration_tag(
    us_gaap: Dict[str, Any],
    candidate_tags: List[str],
    form_filter: str = "10-K",
    unit_keys: tuple[str, ...] = ("USD", "USD/shares"),
) -> Dict[str, Dict[str, Any]]:
    """Extract duration-matched XBRL facts keyed by `end` date string."""
    min_days = 330 if form_filter == "10-K" else 75
    max_days = 380 if form_filter == "10-K" else 105
    period_map: Dict[str, Dict[str, Any]] = {}

    for tag in candidate_tags:
        concept = us_gaap.get(tag)
        if not concept:
            continue
        units = concept.get("units", {})
        for u_key in unit_keys:
            for item in units.get(u_key, []):
                if item.get("form") != form_filter:
                    continue
                start_str, end_str = item.get("start"), item.get("end")
                val = item.get("val")
                if not start_str or not end_str or val is None:
                    continue
                try:
                    days = (date.fromisoformat(end_str) - date.fromisoformat(start_str)).days
                except ValueError:
                    continue
                if not (min_days <= days <= max_days):
                    continue

                existing = period_map.get(end_str)
                if existing is None or (
                    existing["_tag"] == tag and item.get("filed", "") >= existing.get("filed", "")
                ):
                    period_map[end_str] = {**item, "_tag": tag}
    return period_map


def parse_income_statements(
    company_facts: Dict[str, Any],
    form_filter: str = "10-K",
    max_periods: int = 5,
) -> List[IncomeStatementPeriod]:
    """Parse raw SEC `companyfacts` JSON into chronologically sorted `IncomeStatementPeriod` models."""
    us_gaap = company_facts.get("facts", {}).get("us-gaap", {})
    if not us_gaap:
        return []

    extracted = {
        field: _extract_duration_tag(us_gaap, tags, form_filter=form_filter)
        for field, tags in INCOME_STATEMENT_TAGS.items()
    }

    # Anchor on revenue or net_income
    anchor_dates = sorted(
        set(extracted["revenue"].keys()) | set(extracted["net_income"].keys()),
        reverse=True,
    )

    statements: List[IncomeStatementPeriod] = []
    for end_str in anchor_dates:
        rev_fact = extracted["revenue"].get(end_str)
        ni_fact = extracted["net_income"].get(end_str)
        base_fact = rev_fact or ni_fact
        if not base_fact:
            continue

        def _val(field_name: str, default: Optional[float] = None) -> Optional[float]:
            fact = extracted[field_name].get(end_str)
            return float(fact["val"]) if fact and fact.get("val") is not None else default

        revenue = _val("revenue", 0.0) or 0.0
        net_income = _val("net_income", 0.0) or 0.0
        operating_income = _val("operating_income")
        if operating_income is None:
            operating_income = net_income

        da = _val("depreciation_amortization")
        ebitda = (operating_income + da) if da is not None else operating_income

        fp_raw = str(base_fact.get("fp", "FY")).upper()
        fp_enum = FiscalPeriod(fp_raw) if fp_raw in FiscalPeriod._value2member_map_ else FiscalPeriod.FY
        fy_val = int(base_fact.get("fy") or int(end_str[:4]))

        statements.append(
            IncomeStatementPeriod(
                period_end=date.fromisoformat(end_str),
                fiscal_year=fy_val,
                fiscal_period=fp_enum,
                form=FilingForm(form_filter),
                revenue=revenue,
                gross_profit=_val("gross_profit"),
                operating_income=operating_income,
                interest_expense=_val("interest_expense"),
                income_tax_expense=_val("income_tax_expense"),
                net_income=net_income,
                ebitda=ebitda,
                eps_diluted=_val("eps_diluted"),
            )
        )

    statements.sort(key=lambda s: s.period_end, reverse=True)
    return statements[:max_periods]


# ==============================================================================
# Profitability Margin & Multi-Year Growth Calculators
# Computes Gross/Operating/Net margins, EBITDA, and 3-year Revenue CAGR.
# ==============================================================================
def compute_income_metrics(
    income_statements: List[IncomeStatementPeriod],
) -> Dict[str, Optional[float]]:
    """Compute operating/net margins, EBITDA, and multi-year revenue CAGR from income statements."""
    if not income_statements:
        return {
            "gross_margin": None,
            "operating_margin": None,
            "net_margin": None,
            "ebitda_latest": None,
            "revenue_cagr_3y": None,
        }

    latest = income_statements[0]
    gross_margin: Optional[float] = None
    operating_margin: Optional[float] = None
    net_margin: Optional[float] = None

    if latest.revenue > 0:
        if latest.gross_profit is not None:
            gross_margin = round(latest.gross_profit / latest.revenue, 4)
        operating_margin = round(latest.operating_income / latest.revenue, 4)
        net_margin = round(latest.net_income / latest.revenue, 4)

    # Compute 3-year (or available multi-year) Revenue Compound Annual Growth Rate
    revenue_cagr_3y: Optional[float] = None
    if len(income_statements) >= 2:
        # Prefer 3 fiscal years back (index 3) or earliest available up to 3 years back
        lookback_idx = min(3, len(income_statements) - 1)
        oldest = income_statements[lookback_idx]
        years = max(1, latest.fiscal_year - oldest.fiscal_year) or lookback_idx
        if oldest.revenue > 0 and latest.revenue > 0:
            revenue_cagr_3y = round((latest.revenue / oldest.revenue) ** (1.0 / years) - 1.0, 4)

    return {
        "gross_margin": gross_margin,
        "operating_margin": operating_margin,
        "net_margin": net_margin,
        "ebitda_latest": latest.ebitda if latest.ebitda is not None else latest.operating_income,
        "revenue_cagr_3y": revenue_cagr_3y,
    }

