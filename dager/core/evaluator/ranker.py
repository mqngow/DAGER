"""Multi-factor composite scoring and multi-horizon (1m, 3m, 6m, 1y, 3y, 5y) thesis generator."""

from datetime import datetime, timezone
from typing import Dict, List, Optional
from dager.core.evaluator.valuation import compute_valuation_breakdown
from dager.core.fundamentals.ratios import compute_fundamental_breakdown
from dager.schemas.evaluation import (
    FundamentalBreakdown,
    HorizonSignal,
    InvestmentHorizon,
    InvestmentVerdict,
    StockEvaluationReport,
    ValuationBreakdown,
)
from dager.schemas.filings import CompanyFinancials, MarketPriceSnapshot
from dager.schemas.sentiment import CompanySentimentSummary


# ==============================================================================
# Pillar Weight Constants & Multi-Horizon Weight Matrix
# Shifts emphasis from News Sentiment & Price Momentum in short horizons (`1m`,
# `3m`) to Balance Sheet Solvency, ROIC & DCF Valuation in long horizons (`1y`,
# `3y`, `5y`) — requiring zero extra external API calls.
# ==============================================================================
FUNDAMENTALS_WEIGHT = 0.50
VALUATION_WEIGHT = 0.30
SENTIMENT_WEIGHT = 0.20

HORIZON_WEIGHT_PROFILES: Dict[str, tuple[float, float, float, str]] = {
    "1m": (0.20, 0.25, 0.55, "Driven by FinBERT news catalysts, 1-month price momentum, and near-term liquidity."),
    "3m": (0.30, 0.30, 0.40, "Driven by quarterly earnings narrative, 3-month trend, and working capital health."),
    "6m": (0.40, 0.35, 0.25, "Balanced across balance sheet strength, valuation re-rating, and sentiment trend."),
    "1y": (0.50, 0.35, 0.15, "Anchored on annual Free Cash Flow generation, ROIC, and fair-value convergence."),
    "3y": (0.65, 0.30, 0.05, "Anchored on multi-year ROIC compounding, debt solvency, and DCF intrinsic value."),
    "5y": (0.75, 0.20, 0.05, "Anchored on secular balance sheet durability, capital allocation, and long-term compounding."),
}


def classify_verdict(composite_score: float, balance_sheet_score: float) -> InvestmentVerdict:
    """Map composite 0–100 score to a discrete recommendation tier with a balance-sheet safety gate."""
    # Safety gate: never issue Strong Buy if balance sheet health is distressed (< 45)
    if composite_score >= 75.0 and balance_sheet_score >= 45.0:
        return InvestmentVerdict.STRONG_BUY
    if composite_score >= 60.0 and balance_sheet_score >= 35.0:
        return InvestmentVerdict.BUY
    if composite_score >= 45.0:
        return InvestmentVerdict.HOLD
    return InvestmentVerdict.AVOID


# ==============================================================================
# Multi-Horizon Signal Calculator (`1m`, `3m`, `6m`, `1y`, `3y`, `5y`)
# Blends FinBERT sentiment with trailing price momentum for short-term windows
# and progressively transitions to ROIC, FCF, and balance sheet durability for
# multi-year holding periods.
# ==============================================================================
def _momentum_adjusted_sentiment(
    base_sentiment: float,
    snapshot: Optional[MarketPriceSnapshot],
    horizon: str,
) -> float:
    """Blend FinBERT sentiment (0–100) with trailing price return momentum for `1m`–`6m` windows."""
    if snapshot is None:
        return base_sentiment

    ret_map = {
        "1m": snapshot.price_return_1m if snapshot.price_return_1m is not None else snapshot.price_return_6m,
        "3m": snapshot.price_return_3m if snapshot.price_return_3m is not None else snapshot.price_return_6m,
        "6m": snapshot.price_return_6m,
        "1y": snapshot.price_return_1y,
        "3y": snapshot.price_return_3y,
        "5y": snapshot.price_return_5y if snapshot.price_return_5y is not None else snapshot.price_return_3y,
    }
    ret = ret_map.get(horizon)
    if ret is None:
        return base_sentiment

    # Map trailing return into a [20, 80] momentum sub-score centered at 50
    momentum_score = max(20.0, min(80.0, 50.0 + (ret * 100.0)))
    if horizon in ("1m", "3m"):
        return round(0.65 * base_sentiment + 0.35 * momentum_score, 2)
    return round(0.80 * base_sentiment + 0.20 * momentum_score, 2)


def compute_horizon_signals(
    fundamentals: FundamentalBreakdown,
    valuation: ValuationBreakdown,
    sentiment_score: float,
    snapshot: Optional[MarketPriceSnapshot] = None,
) -> Dict[str, HorizonSignal]:
    """Compute tailored scores and Buy/Hold/Avoid verdicts across `1m`, `3m`, `6m`, `1y`, `3y`, and `5y`."""
    signals: Dict[str, HorizonSignal] = {}

    for horizon_key, (w_fund, w_val, w_sent, summary_text) in HORIZON_WEIGHT_PROFILES.items():
        # Long horizons (3y, 5y) emphasize balance sheet solvency & ROIC durability even further
        if horizon_key == "5y":
            eff_fund = 0.60 * fundamentals.balance_sheet_score + 0.40 * fundamentals.profitability_score
        else:
            eff_fund = fundamentals.pillar_score

        eff_sent = _momentum_adjusted_sentiment(sentiment_score, snapshot, horizon_key)
        score = round(
            max(0.0, min(100.0, (w_fund * eff_fund) + (w_val * valuation.pillar_score) + (w_sent * eff_sent))),
            2,
        )
        verdict = classify_verdict(score, fundamentals.balance_sheet_score)

        signals[horizon_key] = HorizonSignal(
            horizon=horizon_key,
            score=score,
            verdict=verdict,
            fundamentals_weight=w_fund,
            valuation_weight=w_val,
            sentiment_momentum_weight=w_sent,
            driver_summary=summary_text,
        )

    return signals


# ==============================================================================
# Automated Investment Thesis & Bull/Bear Case Synthesizer
# Generates human-readable bullet points and an executive summary explaining
# the 6-month to 3-year investment verdict.
# ==============================================================================
def _build_thesis(
    company_name: str,
    ticker: str,
    verdict: InvestmentVerdict,
    composite_score: float,
    fundamentals: FundamentalBreakdown,
    valuation: ValuationBreakdown,
    sentiment: Optional[CompanySentimentSummary],
) -> tuple[List[str], List[str], str]:
    """Construct `bull_case`, `bear_case`, and `thesis_summary` strings from pillar metrics."""
    bull_case: List[str] = []
    bear_case: List[str] = []

    # 1. Balance Sheet & Fundamental Signals
    if fundamentals.current_ratio is not None:
        if fundamentals.current_ratio >= 1.5:
            bull_case.append(f"Strong short-term liquidity with a {fundamentals.current_ratio:.2f}x current ratio.")
        elif fundamentals.current_ratio < 1.0:
            bear_case.append(f"Tight working capital coverage ({fundamentals.current_ratio:.2f}x current ratio).")

    if fundamentals.debt_to_equity is not None:
        if fundamentals.debt_to_equity <= 0.6:
            bull_case.append(f"Conservative balance sheet leverage (Debt/Equity of {fundamentals.debt_to_equity:.2f}x).")
        elif fundamentals.debt_to_equity > 2.0:
            bear_case.append(f"Elevated debt load relative to book equity ({fundamentals.debt_to_equity:.2f}x Debt/Equity).")

    if fundamentals.roic is not None:
        if fundamentals.roic >= 0.15:
            bull_case.append(f"High capital efficiency with {fundamentals.roic * 100:.1f}% Return on Invested Capital (ROIC).")
        elif fundamentals.roic < 0.05:
            bear_case.append(f"Sub-par Return on Invested Capital ({fundamentals.roic * 100:.1f}% ROIC).")

    if fundamentals.free_cash_flow_latest is not None:
        if fundamentals.free_cash_flow_latest > 0:
            bull_case.append("Positive Free Cash Flow generation supports self-funded long-term growth.")
        else:
            bear_case.append("Negative Free Cash Flow requires close monitoring of cash runway.")

    # 2. Valuation & Price Action Signals
    if valuation.implied_upside_pct is not None:
        if valuation.implied_upside_pct >= 15.0:
            bull_case.append(
                f"Attractive valuation offering ~{valuation.implied_upside_pct:.1f}% implied upside to estimated fair value (${valuation.estimated_fair_value:.2f})."
            )
        elif valuation.implied_upside_pct <= -15.0:
            bear_case.append(
                f"Shares trade at a premium ({abs(valuation.implied_upside_pct):.1f}% above estimated intrinsic value of ${valuation.estimated_fair_value:.2f})."
            )

    # 3. News & Qualitative Sentiment Signals
    if sentiment:
        if sentiment.sentiment_score_0_100 >= 60.0:
            bull_case.append(
                f"Constructive news and narrative momentum (Sentiment score: {sentiment.sentiment_score_0_100:.1f}/100)."
            )
        elif sentiment.sentiment_score_0_100 <= 40.0:
            bear_case.append(
                f"Near-term narrative headwinds in recent news/filings (Sentiment score: {sentiment.sentiment_score_0_100:.1f}/100)."
            )
        for headline in sentiment.top_bullish_headlines[:1]:
            bull_case.append(f"Recent catalyst: \"{headline}\"")
        for headline in sentiment.top_bearish_headlines[:1]:
            bear_case.append(f"Recent risk signal: \"{headline}\"")

    if not bull_case:
        bull_case.append("Stable operational footprint with established SEC reporting history.")
    if not bear_case:
        bear_case.append("Standard macroeconomic and sector-level execution risks over a 1m–5y horizon.")

    upside_str = (
        f"an estimated fair value of ${valuation.estimated_fair_value:.2f} ({valuation.implied_upside_pct:+.1f}% vs. ${valuation.current_price:.2f})"
        if valuation.estimated_fair_value and valuation.implied_upside_pct is not None
        else f"a current market price of ${valuation.current_price:.2f}"
    )
    thesis_summary = (
        f"{company_name} ({ticker}) is rated '{verdict.value}' overall across a 1-month to 5-year (1m–5y) horizon "
        f"with a composite score of {composite_score:.1f}/100. "
        f"Fundamentals score {fundamentals.pillar_score:.1f}/100 (Balance Sheet: {fundamentals.balance_sheet_score:.1f}/100), "
        f"paired with {upside_str}."
    )
    return bull_case, bear_case, thesis_summary


# ==============================================================================
# End-to-End Company Evaluator Entrypoint
# Synthesizes `CompanyFinancials` and `CompanySentimentSummary` into the final
# `StockEvaluationReport` schema.
# ==============================================================================
def evaluate_company(
    financials: CompanyFinancials,
    sentiment: Optional[CompanySentimentSummary] = None,
    was_cached: bool = False,
) -> StockEvaluationReport:
    """Run all three evaluation pillars and return a complete `StockEvaluationReport`."""
    fundamentals = compute_fundamental_breakdown(financials)
    valuation = compute_valuation_breakdown(financials)
    sentiment_score = sentiment.sentiment_score_0_100 if sentiment is not None else 50.0

    composite_score = round(
        (FUNDAMENTALS_WEIGHT * fundamentals.pillar_score)
        + (VALUATION_WEIGHT * valuation.pillar_score)
        + (SENTIMENT_WEIGHT * sentiment_score),
        2,
    )
    verdict = classify_verdict(composite_score, fundamentals.balance_sheet_score)
    horizon_signals = compute_horizon_signals(
        fundamentals=fundamentals,
        valuation=valuation,
        sentiment_score=sentiment_score,
        snapshot=financials.market_snapshot,
    )

    bull_case, bear_case, thesis_summary = _build_thesis(
        company_name=financials.company.company_name,
        ticker=financials.company.ticker,
        verdict=verdict,
        composite_score=composite_score,
        fundamentals=fundamentals,
        valuation=valuation,
        sentiment=sentiment,
    )

    return StockEvaluationReport(
        ticker=financials.company.ticker,
        cik=financials.company.cik,
        company_name=financials.company.company_name,
        is_sp500=financials.company.is_sp500,
        was_cached=was_cached,
        evaluated_at=datetime.now(timezone.utc),
        horizon=InvestmentHorizon.ONE_MONTH_TO_FIVE_YEARS,
        composite_score=composite_score,
        verdict=verdict,
        horizon_signals=horizon_signals,
        fundamentals=fundamentals,
        valuation=valuation,
        sentiment_score=sentiment_score,
        bull_case=bull_case,
        bear_case=bear_case,
        thesis_summary=thesis_summary,
    )

