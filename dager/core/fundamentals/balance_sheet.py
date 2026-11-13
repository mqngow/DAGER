"""Working capital, debt structures, liquidity ratios, and SEC XBRL balance sheet extraction."""

from datetime import date
from typing import Any, Dict, List, Optional
from dager.schemas.filings import BalanceSheetPeriod, FilingForm, FiscalPeriod


# ==============================================================================
# SEC XBRL `us-gaap` Tag Dictionary for Balance Sheet Items
# Maps standardized balance sheet fields to ordered fallback lists of SEC XBRL
# concept names so differences across industries and filing years are handled.
# ==============================================================================
BALANCE_SHEET_TAGS: Dict[str, List[str]] = {
    "cash_and_equivalents": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsAndShortTermInvestments",
        "CashAndDueFromBanks",
    ],
    "accounts_receivable": [
        "AccountsReceivableNetCurrent",
        "ReceivablesNetCurrent",
    ],
    "inventory": [
        "InventoryNet",
        "InventoryFinishedGoodsNetOfReserves",
    ],
    "current_assets": [
        "AssetsCurrent",
    ],
    "total_assets": [
        "Assets",
    ],
    "accounts_payable": [
        "AccountsPayableCurrent",
        "AccountsPayableAndAccruedLiabilitiesCurrent",
    ],
    "short_term_debt": [
        "DebtCurrent",
        "LongTermDebtCurrent",
        "ShortTermBorrowings",
        "CommercialPaper",
    ],
    "current_liabilities": [
        "LiabilitiesCurrent",
    ],
    "long_term_debt": [
        "LongTermDebtNoncurrent",
        "LongTermDebt",
        "LongTermDebtAndCapitalLeaseObligations",
    ],
    "total_liabilities": [
        "Liabilities",
    ],
    "stockholders_equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "shares_outstanding": [
        "CommonStockSharesOutstanding",
        "EntityCommonStockSharesOutstanding",
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingBasic",
    ],
}


# ==============================================================================
# XBRL Point-in-Time Fact Extractor
# Indexes instant/point-in-time `us-gaap` facts by `(end_date, form)` from raw
# SEC `companyfacts` JSON and builds normalized `BalanceSheetPeriod` models.
# ==============================================================================
def _extract_tag_by_period(
    us_gaap: Dict[str, Any],
    candidate_tags: List[str],
    unit_keys: tuple[str, ...] = ("USD", "shares"),
) -> Dict[tuple[str, str], Dict[str, Any]]:
    """Return a mapping of `(end_date, form)` -> latest filed XBRL fact dict across candidate tags."""
    period_map: Dict[tuple[str, str], Dict[str, Any]] = {}
    for tag in candidate_tags:
        concept = us_gaap.get(tag)
        if not concept:
            continue
        units = concept.get("units", {})
        for u_key in unit_keys:
            for item in units.get(u_key, []):
                form = item.get("form")
                end_str = item.get("end")
                val = item.get("val")
                if form not in ("10-K", "10-Q") or not end_str or val is None:
                    continue
                # Skip zero/negative share counts (e.g., retired/secondary share classes)
                if u_key == "shares" and float(val) <= 0:
                    continue
                key = (end_str, form)
                existing = period_map.get(key)
                # Prefer the first matching priority tag, or the most recently filed revision
                if existing is None or (
                    existing["_tag"] == tag and item.get("filed", "") >= existing.get("filed", "")
                ):
                    period_map[key] = {**item, "_tag": tag}
    return period_map


def parse_balance_sheets(
    company_facts: Dict[str, Any],
    form_filter: Optional[str] = "10-K",
    max_periods: int = 5,
) -> List[BalanceSheetPeriod]:
    """Parse raw SEC `companyfacts` JSON into chronologically sorted `BalanceSheetPeriod` models."""
    us_gaap = company_facts.get("facts", {}).get("us-gaap", {})
    if not us_gaap:
        return []

    extracted = {
        field: _extract_tag_by_period(us_gaap, tags)
        for field, tags in BALANCE_SHEET_TAGS.items()
    }

    # Anchor periods on `total_assets` (every valid balance sheet must report total assets)
    anchor_periods = extracted["total_assets"]
    periods: List[BalanceSheetPeriod] = []

    for (end_str, form_str), asset_fact in anchor_periods.items():
        if form_filter and form_str != form_filter:
            continue

        def _val(field_name: str, default: Optional[float] = None) -> Optional[float]:
            fact = extracted[field_name].get((end_str, form_str))
            return float(fact["val"]) if fact and fact.get("val") is not None else default

        total_assets = _val("total_assets", 0.0) or 0.0
        if total_assets <= 0:
            continue

        stockholders_equity = _val("stockholders_equity")
        total_liabilities = _val("total_liabilities")

        # Accounting identity fallback: Assets = Liabilities + Stockholders' Equity
        if total_liabilities is None and stockholders_equity is not None:
            total_liabilities = total_assets - stockholders_equity
        elif stockholders_equity is None and total_liabilities is not None:
            stockholders_equity = total_assets - total_liabilities
        elif total_liabilities is None and stockholders_equity is None:
            total_liabilities = 0.0
            stockholders_equity = total_assets

        current_assets = _val("current_assets") or (_val("cash_and_equivalents", 0.0) or 0.0)
        current_liabilities = _val("current_liabilities") or (_val("short_term_debt", 0.0) or 0.0)

        fp_raw = str(asset_fact.get("fp", "FY")).upper()
        fp_enum = FiscalPeriod(fp_raw) if fp_raw in FiscalPeriod._value2member_map_ else FiscalPeriod.FY
        fy_val = int(asset_fact.get("fy") or int(end_str[:4]))
        filed_str = asset_fact.get("filed")
        raw_shares = _val("shares_outstanding")

        periods.append(
            BalanceSheetPeriod(
                period_end=date.fromisoformat(end_str),
                fiscal_year=fy_val,
                fiscal_period=fp_enum,
                form=FilingForm(form_str),
                filed_date=date.fromisoformat(filed_str) if filed_str else None,
                cash_and_equivalents=_val("cash_and_equivalents", 0.0) or 0.0,
                accounts_receivable=_val("accounts_receivable"),
                inventory=_val("inventory"),
                current_assets=current_assets,
                total_assets=total_assets,
                accounts_payable=_val("accounts_payable"),
                short_term_debt=_val("short_term_debt", 0.0) or 0.0,
                current_liabilities=current_liabilities,
                long_term_debt=_val("long_term_debt", 0.0) or 0.0,
                total_liabilities=total_liabilities,
                stockholders_equity=stockholders_equity,
                shares_outstanding=raw_shares if (raw_shares is not None and raw_shares > 0) else None,
            )
        )

    # Sort newest period_end first and deduplicate by fiscal year when filtering 10-K
    periods.sort(key=lambda p: p.period_end, reverse=True)
    return periods[:max_periods]


# ==============================================================================
# Balance Sheet Liquidity, Solvency & Health Scoring Calculators
# Computes Current Ratio, Quick Ratio, Cash-to-Short-Term-Debt, Debt-to-Equity,
# YoY Book Value growth, and a normalized 0–100 Balance Sheet Health Score.
# ==============================================================================
def compute_balance_sheet_metrics(
    balance_sheets: List[BalanceSheetPeriod],
) -> Dict[str, Optional[float]]:
    """Compute liquidity, solvency, and book value growth metrics plus a 0–100 health score."""
    if not balance_sheets:
        return {
            "current_ratio": None,
            "quick_ratio": None,
            "cash_to_short_term_debt": None,
            "debt_to_equity": None,
            "book_value_growth_yoy": None,
            "balance_sheet_score": 50.0,
        }

    latest = balance_sheets[0]

    # 1. Current Ratio = Current Assets / Current Liabilities
    current_ratio: Optional[float] = None
    if latest.current_liabilities > 0:
        current_ratio = round(latest.current_assets / latest.current_liabilities, 3)

    # 2. Quick Ratio = (Cash + Receivables, or Current Assets - Inventory) / Current Liabilities
    quick_ratio: Optional[float] = None
    if latest.current_liabilities > 0:
        liquid_assets = (
            latest.current_assets - (latest.inventory or 0.0)
            if latest.inventory is not None
            else latest.cash_and_equivalents + (latest.accounts_receivable or 0.0)
        )
        quick_ratio = round(max(0.0, liquid_assets) / latest.current_liabilities, 3)

    # 3. Cash-to-Short-Term-Debt Coverage
    cash_to_st_debt: Optional[float] = None
    if latest.short_term_debt > 0:
        cash_to_st_debt = round(latest.cash_and_equivalents / latest.short_term_debt, 3)
    elif latest.cash_and_equivalents > 0:
        cash_to_st_debt = 10.0  # Debt-free short-term obligations

    # 4. Debt-to-Equity Ratio = Total Debt / Stockholders' Equity
    debt_to_equity: Optional[float] = None
    if latest.stockholders_equity > 0:
        debt_to_equity = round(latest.total_debt / latest.stockholders_equity, 3)

    # 5. YoY Book Value (Stockholders' Equity) Growth
    book_value_growth_yoy: Optional[float] = None
    if len(balance_sheets) >= 2:
        prior_eq = balance_sheets[1].stockholders_equity
        if prior_eq > 0:
            book_value_growth_yoy = round(
                (latest.stockholders_equity - prior_eq) / prior_eq, 4
            )

    # 6. Composite Balance Sheet Sub-Score (0–100)
    # Evaluates liquidity cushion, leverage discipline, and net cash position
    score = 50.0
    if current_ratio is not None:
        if current_ratio >= 2.0:
            score += 15.0
        elif current_ratio >= 1.2:
            score += 10.0
        elif current_ratio < 0.9:
            score -= 12.0

    if quick_ratio is not None:
        if quick_ratio >= 1.2:
            score += 10.0
        elif quick_ratio < 0.7:
            score -= 8.0

    if debt_to_equity is not None:
        if debt_to_equity <= 0.5:
            score += 15.0
        elif debt_to_equity <= 1.2:
            score += 7.0
        elif debt_to_equity > 2.5:
            score -= 15.0
    elif latest.stockholders_equity <= 0:
        score -= 20.0

    if latest.net_debt < 0:
        # Net cash balance sheet bonus
        score += 10.0

    if book_value_growth_yoy is not None:
        if book_value_growth_yoy > 0.05:
            score += 5.0
        elif book_value_growth_yoy < -0.10:
            score -= 5.0

    return {
        "current_ratio": current_ratio,
        "quick_ratio": quick_ratio,
        "cash_to_short_term_debt": cash_to_st_debt,
        "debt_to_equity": debt_to_equity,
        "book_value_growth_yoy": book_value_growth_yoy,
        "balance_sheet_score": round(max(0.0, min(100.0, score)), 2),
    }

