"""Fair value estimation, price multiples, and 6-month to 3-year margin-of-safety checks."""

from typing import Optional
from dager.schemas.evaluation import ValuationBreakdown
from dager.schemas.filings import CompanyFinancials


# ==============================================================================
# Intrinsic Fair Value Estimator (Blended DCF & Normalized Earnings Multiple)
# Estimates per-share intrinsic value over a 1-month to 5-year horizon by
# combining:
# 1. A 5-year Free Cash Flow (DCF) model with terminal value and net debt adjustment.
# 2. A normalized 18x P/E (or EV/EBITDA) multiple on recent earnings.
# ==============================================================================
def estimate_intrinsic_fair_value(
    financials: CompanyFinancials,
    discount_rate: float = 0.10,
    terminal_growth: float = 0.03,
) -> Optional[float]:
    """Estimate intrinsic fair value per share (USD) from FCF, earnings, and net debt."""
    snap = financials.market_snapshot
    bs = financials.balance_sheets[0] if financials.balance_sheets else None
    inc = financials.income_statements[0] if financials.income_statements else None
    cf = financials.cash_flows[0] if financials.cash_flows else None

    # Determine effective share count from balance sheet or market cap / price
    shares: Optional[float] = bs.shares_outstanding if bs and bs.shares_outstanding else None
    if (not shares or shares <= 0) and snap and snap.market_cap and snap.current_price > 0:
        shares = snap.market_cap / snap.current_price
    if not shares or shares <= 0:
        return None

    net_debt = bs.net_debt if bs else 0.0
    estimates = []

    # Method 1: 5-year Discounted Free Cash Flow (DCF)
    if cf and cf.free_cash_flow > 0:
        fcf = cf.free_cash_flow
        # Clamp projected annual FCF growth between 3% and 16%
        growth_rate = 0.07
        if len(financials.income_statements) >= 2:
            prior_rev = financials.income_statements[-1].revenue
            if prior_rev > 0 and inc and inc.revenue > 0:
                years = max(1, inc.fiscal_year - financials.income_statements[-1].fiscal_year)
                raw_cagr = (inc.revenue / prior_rev) ** (1.0 / years) - 1.0
                growth_rate = max(0.03, min(0.16, raw_cagr))

        pv_fcf = sum(
            (fcf * ((1.0 + growth_rate) ** yr)) / ((1.0 + discount_rate) ** yr)
            for yr in range(1, 6)
        )
        terminal_fcf = fcf * ((1.0 + growth_rate) ** 5) * (1.0 + terminal_growth)
        terminal_val = terminal_fcf / (discount_rate - terminal_growth)
        pv_terminal = terminal_val / ((1.0 + discount_rate) ** 5)

        equity_val_dcf = (pv_fcf + pv_terminal) - net_debt
        if equity_val_dcf > 0:
            estimates.append(equity_val_dcf / shares)

    # Method 2: Normalized Earnings Fair Value (18x Net Income adjusted for Net Cash/Debt)
    if inc and inc.net_income > 0:
        equity_val_pe = (inc.net_income * 18.0) - (net_debt * 0.5)
        if equity_val_pe > 0:
            estimates.append(equity_val_pe / shares)

    if not estimates:
        return None

    return round(sum(estimates) / len(estimates), 2)


# ==============================================================================
# Pillar 2 Valuation Breakdown & Margin-of-Safety Calculator
# Compares current market price against intrinsic fair value and standard
# valuation multiples (P/E, EV/EBITDA, FCF Yield) to score Pillar 2 (0–100).
# ==============================================================================
def compute_valuation_breakdown(financials: CompanyFinancials) -> ValuationBreakdown:
    """Compute valuation multiples, margin of safety, 6m–3y upside, and Pillar 2 score."""
    snap = financials.market_snapshot
    if not snap or snap.current_price <= 0:
        raise ValueError("A valid `MarketPriceSnapshot` with `current_price > 0` is required for valuation.")

    price = snap.current_price
    bs = financials.balance_sheets[0] if financials.balance_sheets else None
    inc = financials.income_statements[0] if financials.income_statements else None
    cf = financials.cash_flows[0] if financials.cash_flows else None

    # Derive market cap and enterprise value if missing from the snapshot
    shares = bs.shares_outstanding if bs and bs.shares_outstanding else None
    market_cap = snap.market_cap or ((price * shares) if shares else None)
    net_debt = bs.net_debt if bs else 0.0
    enterprise_value = snap.enterprise_value or ((market_cap + net_debt) if market_cap else None)

    # 1. Valuation Multiples (P/E, EV/EBITDA, FCF Yield)
    pe_ratio: Optional[float] = None
    if inc and inc.eps_diluted and inc.eps_diluted > 0:
        pe_ratio = round(price / inc.eps_diluted, 2)
    elif market_cap and inc and inc.net_income > 0:
        pe_ratio = round(market_cap / inc.net_income, 2)

    ev_to_ebitda: Optional[float] = None
    ebitda = (inc.ebitda or inc.operating_income) if inc else None
    if enterprise_value and enterprise_value > 0 and ebitda and ebitda > 0:
        ev_to_ebitda = round(enterprise_value / ebitda, 2)

    fcf_yield: Optional[float] = None
    if market_cap and market_cap > 0 and cf:
        fcf_yield = round(cf.free_cash_flow / market_cap, 4)

    # 2. Intrinsic Fair Value & Margin of Safety
    fair_value = estimate_intrinsic_fair_value(financials)
    margin_of_safety_pct: Optional[float] = None
    implied_upside_pct: Optional[float] = None

    if fair_value and fair_value > 0:
        margin_of_safety_pct = round(((fair_value - price) / fair_value) * 100.0, 2)
        implied_upside_pct = round(((fair_value - price) / price) * 100.0, 2)

    # 3. Pillar 2 Valuation Score (0–100)
    score = 50.0
    if implied_upside_pct is not None:
        if implied_upside_pct >= 25.0:
            score += 22.0
        elif implied_upside_pct >= 10.0:
            score += 12.0
        elif implied_upside_pct <= -25.0:
            score -= 18.0
        elif implied_upside_pct <= -10.0:
            score -= 8.0

    if fcf_yield is not None:
        if fcf_yield >= 0.05:
            score += 14.0
        elif fcf_yield >= 0.03:
            score += 7.0
        elif fcf_yield < 0.0:
            score -= 12.0

    if pe_ratio is not None:
        if 8.0 <= pe_ratio <= 20.0:
            score += 10.0
        elif 20.0 < pe_ratio <= 28.0:
            score += 3.0
        elif pe_ratio > 45.0:
            score -= 10.0

    if ev_to_ebitda is not None:
        if ev_to_ebitda <= 14.0:
            score += 6.0
        elif ev_to_ebitda > 30.0:
            score -= 6.0

    pillar_score = round(max(0.0, min(100.0, score)), 2)

    return ValuationBreakdown(
        current_price=round(price, 2),
        estimated_fair_value=fair_value,
        margin_of_safety_pct=margin_of_safety_pct,
        implied_upside_pct=implied_upside_pct,
        pe_ratio=pe_ratio,
        ev_to_ebitda=ev_to_ebitda,
        fcf_yield=fcf_yield,
        pillar_score=pillar_score,
    )

