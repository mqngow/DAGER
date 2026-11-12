"""Multi-factor composite scoring (Fundamentals + Valuation + Sentiment) and 6m–3y thesis generator."""

from datetime import datetime, timezone
from typing import List, Optional
from dager.core.evaluator.valuation import compute_valuation_breakdown
from dager.core.fundamentals.ratios import compute_fundamental_breakdown
from dager.schemas.evaluation import (
    FundamentalBreakdown,
    InvestmentHorizon,
    InvestmentVerdict,
    StockEvaluationReport,
    ValuationBreakdown,
)
from dager.schemas.filings import CompanyFinancials
from dager.schemas.sentiment import CompanySentimentSummary


# ==============================================================================
# Pillar Weight Constants & Verdict Thresholds
# Gives primary weight (50%) to SEC Balance Sheet & Fundamental Strength,
# 30% to Valuation & Margin of Safety, and 20% to News/Transcript Sentiment.
# ==============================================================================
FUNDAMENTALS_WEIGHT = 0.50
VALUATION_WEIGHT = 0.30
SENTIMENT_WEIGHT = 0.20


def classify_verdict(composite_score: float, balance_sheet_score: float) -> InvestmentVerdict:
    """Map composite 0–100 score to a discrete recommendation tier with a balance-sheet safety gate."""
    # Safety gate: never issue Strong Buy if balance sheet health is distressed (< 40)
    if composite_score >= 75.0 and balance_sheet_score >= 45.0:
        return InvestmentVerdict.STRONG_BUY
    if composite_score >= 60.0 and balance_sheet_score >= 35.0:
        return InvestmentVerdict.BUY
    if composite_score >= 45.0:
        return InvestmentVerdict.HOLD
    return InvestmentVerdict.AVOID


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
        bear_case.append("Standard macroeconomic and sector-level execution risks over a 6m–3y horizon.")

    upside_str = (
        f"an estimated fair value of ${valuation.estimated_fair_value:.2f} ({valuation.implied_upside_pct:+.1f}% vs. ${valuation.current_price:.2f})"
        if valuation.estimated_fair_value and valuation.implied_upside_pct is not None
        else f"a current market price of ${valuation.current_price:.2f}"
    )
    thesis_summary = (
        f"{company_name} ({ticker}) is rated '{verdict.value}' for a 6-month to 3-year horizon "
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
        horizon=InvestmentHorizon.SIX_MONTHS_TO_THREE_YEARS,
        composite_score=composite_score,
        verdict=verdict,
        fundamentals=fundamentals,
        valuation=valuation,
        sentiment_score=sentiment_score,
        bull_case=bull_case,
        bear_case=bear_case,
        thesis_summary=thesis_summary,
    )

