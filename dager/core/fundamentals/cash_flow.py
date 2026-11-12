"""Free cash flow (FCF), CapEx intensity, operational runway, and SEC XBRL cash flow extraction."""

from datetime import date
from typing import Any, Dict, List, Optional
from dager.core.fundamentals.income import _extract_duration_tag
from dager.schemas.filings import CashFlowPeriod, FilingForm, FiscalPeriod


# ==============================================================================
# SEC XBRL `us-gaap` Tag Dictionary for Cash Flow Statement Items
# Covers operating cash flow, capital expenditures, D&A, dividends, and buybacks.
# ==============================================================================
CASH_FLOW_TAGS: Dict[str, List[str]] = {
    "operating_cash_flow": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
    "capital_expenditures": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        "CapitalExpendituresIncurredButNotYetPaid",
    ],
    "depreciation_and_amortization": [
        "DepreciationDepletionAndAmortization",
        "DepreciationAndAmortization",
    ],
    "dividends_paid": [
        "PaymentsOfDividendsCommonStock",
        "PaymentsOfDividends",
    ],
    "share_repurchases": [
        "PaymentsForRepurchaseOfCommonStock",
    ],
}


# ==============================================================================
# XBRL Cash Flow Parser
# Extracts full-year or quarterly cash flow statements into `CashFlowPeriod`
# models (which automatically normalize CapEx as a non-negative magnitude).
# ==============================================================================
def parse_cash_flows(
    company_facts: Dict[str, Any],
    form_filter: str = "10-K",
    max_periods: int = 5,
) -> List[CashFlowPeriod]:
    """Parse raw SEC `companyfacts` JSON into chronologically sorted `CashFlowPeriod` models."""
    us_gaap = company_facts.get("facts", {}).get("us-gaap", {})
    if not us_gaap:
        return []

    extracted = {
        field: _extract_duration_tag(us_gaap, tags, form_filter=form_filter)
        for field, tags in CASH_FLOW_TAGS.items()
    }

    anchor_dates = sorted(extracted["operating_cash_flow"].keys(), reverse=True)
    periods: List[CashFlowPeriod] = []

    for end_str in anchor_dates:
        ocf_fact = extracted["operating_cash_flow"][end_str]

        def _val(field_name: str, default: Optional[float] = None) -> Optional[float]:
            fact = extracted[field_name].get(end_str)
            return float(fact["val"]) if fact and fact.get("val") is not None else default

        fp_raw = str(ocf_fact.get("fp", "FY")).upper()
        fp_enum = FiscalPeriod(fp_raw) if fp_raw in FiscalPeriod._value2member_map_ else FiscalPeriod.FY
        fy_val = int(ocf_fact.get("fy") or int(end_str[:4]))

        periods.append(
            CashFlowPeriod(
                period_end=date.fromisoformat(end_str),
                fiscal_year=fy_val,
                fiscal_period=fp_enum,
                form=FilingForm(form_filter),
                operating_cash_flow=_val("operating_cash_flow", 0.0) or 0.0,
                capital_expenditures=abs(_val("capital_expenditures", 0.0) or 0.0),
                depreciation_and_amortization=_val("depreciation_and_amortization"),
                dividends_paid=abs(_val("dividends_paid", 0.0) or 0.0),
                share_repurchases=abs(_val("share_repurchases", 0.0) or 0.0),
            )
        )

    periods.sort(key=lambda p: p.period_end, reverse=True)
    return periods[:max_periods]


# ==============================================================================
# Free Cash Flow, Conversion Rate & Runway Calculators
# Evaluates cash generation quality, CapEx intensity, and operational cash
# runway (in years) if a company is currently FCF-negative.
# ==============================================================================
def compute_cash_flow_metrics(
    cash_flows: List[CashFlowPeriod],
    latest_net_income: Optional[float] = None,
    latest_revenue: Optional[float] = None,
    cash_on_hand: Optional[float] = None,
) -> Dict[str, Optional[float]]:
    """Compute Free Cash Flow, FCF conversion rate, CapEx intensity, and cash runway."""
    if not cash_flows:
        return {
            "free_cash_flow_latest": None,
            "fcf_conversion_rate": None,
            "capex_intensity": None,
            "cash_runway_years": None,
        }

    latest = cash_flows[0]
    fcf = latest.free_cash_flow

    # 1. FCF Conversion Rate = Free Cash Flow / Net Income
    fcf_conversion: Optional[float] = None
    if latest_net_income is not None and latest_net_income > 0:
        fcf_conversion = round(fcf / latest_net_income, 3)

    # 2. CapEx Intensity = Capital Expenditures / Revenue (or Operating Cash Flow)
    capex_intensity: Optional[float] = None
    if latest_revenue and latest_revenue > 0:
        capex_intensity = round(latest.capital_expenditures / latest_revenue, 4)
    elif latest.operating_cash_flow > 0:
        capex_intensity = round(latest.capital_expenditures / latest.operating_cash_flow, 4)

    # 3. Operational Cash Runway (Years) if burning cash (FCF < 0)
    cash_runway_years: Optional[float] = None
    if fcf >= 0:
        cash_runway_years = 99.0  # Self-sustaining positive free cash flow
    elif cash_on_hand is not None and cash_on_hand > 0:
        annual_burn = abs(fcf)
        cash_runway_years = round(cash_on_hand / annual_burn, 2)

    return {
        "free_cash_flow_latest": round(fcf, 2),
        "fcf_conversion_rate": fcf_conversion,
        "capex_intensity": capex_intensity,
        "cash_runway_years": cash_runway_years,
    }

