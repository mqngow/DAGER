"""Phase 3 tests for SEC XBRL parsing, Balance Sheet & Cash Flow ratios, Sentiment, and 6m–3y Evaluator."""

from dager.core.evaluator import compute_valuation_breakdown, evaluate_company
from dager.core.fundamentals import (
    compute_balance_sheet_metrics,
    compute_cash_flow_metrics,
    compute_fundamental_breakdown,
    compute_income_metrics,
    compute_roic,
    parse_company_financials,
)
from dager.core.sentiment import SentimentScorer, clean_html_text, extract_mda_section
from dager.schemas.evaluation import InvestmentVerdict


# ==============================================================================
# Pillar 1 Tests: XBRL Parsing & Balance Sheet / Income / Cash Flow Calculations
# Verifies that `parse_company_financials` extracts multi-year 10-K periods and
# accurately computes liquidity, leverage, FCF, and ROIC metrics.
# ==============================================================================
def test_xbrl_parsing_and_fundamental_ratios(
    mock_company_identity,
    mock_sec_company_facts,
    mock_market_snapshot,
):
    """Test XBRL parsing and fundamental metric accuracy across Balance Sheet, Income, and Cash Flow."""
    financials = parse_company_financials(
        company=mock_company_identity,
        company_facts=mock_sec_company_facts,
        market_snapshot=mock_market_snapshot,
    )

    assert len(financials.balance_sheets) == 2
    assert len(financials.income_statements) == 2
    assert len(financials.cash_flows) == 2

    # Verify Balance Sheet metrics (FY 2025: CA=145B, CL=110B, Equity=90B, Total Debt=100B)
    bs_metrics = compute_balance_sheet_metrics(financials.balance_sheets)
    assert bs_metrics["current_ratio"] == round(145 / 110, 3)
    assert bs_metrics["debt_to_equity"] == round(100 / 90, 3)
    assert bs_metrics["book_value_growth_yoy"] == 0.2  # (90B - 75B) / 75B

    # Verify Income Statement metrics (FY 2025: Rev=410B, OpInc=125B, NetInc=105B)
    inc_metrics = compute_income_metrics(financials.income_statements)
    assert inc_metrics["operating_margin"] == round(125 / 410, 4)
    assert inc_metrics["net_margin"] == round(105 / 410, 4)
    assert inc_metrics["revenue_cagr_3y"] > 0

    # Verify Cash Flow metrics (FY 2025: OCF=118B, CapEx=10B -> FCF=108B)
    cf_metrics = compute_cash_flow_metrics(
        financials.cash_flows,
        latest_net_income=105_000_000_000,
        latest_revenue=410_000_000_000,
    )
    assert cf_metrics["free_cash_flow_latest"] == 108_000_000_000.0
    assert cf_metrics["fcf_conversion_rate"] == round(108 / 105, 3)

    # Verify ROIC and Pillar 1 FundamentalBreakdown synthesis
    roic = compute_roic(financials)
    assert roic is not None and roic > 0.50

    breakdown = compute_fundamental_breakdown(financials)
    assert 0.0 <= breakdown.pillar_score <= 100.0
    assert breakdown.pillar_score >= 65.0


# ==============================================================================
# Pillar 3 Tests: HTML/MD&A Parser & Sentiment Scorer
# Verifies HTML stripping, MD&A extraction, and time-decay sentiment scoring.
# ==============================================================================
def test_sentiment_parser_and_scorer(sample_sentiment_docs):
    """Verify MD&A extraction and sentiment scoring with recency weighting."""
    raw_html = "<html><body><script>bad()</script><p>Item 7. Management's Discussion and Analysis " + ("Strong revenue growth. " * 25) + "Item 8. Financial Statements</p></body></html>"
    mda = extract_mda_section(raw_html)
    assert "Strong revenue growth" in mda
    assert "bad()" not in clean_html_text(raw_html)

    scorer = SentimentScorer(use_finbert=False)
    summary = scorer.score_documents("AAPL", sample_sentiment_docs)
    assert summary.ticker == "AAPL"
    assert summary.document_count == 2
    assert summary.weighted_polarity > 0
    assert summary.sentiment_score_0_100 > 50.0


# ==============================================================================
# Pillar 2 & End-to-End 6m–3y Evaluator Tests
# Verifies intrinsic valuation and full multi-factor `evaluate_company` report.
# ==============================================================================
def test_end_to_end_company_evaluation(
    mock_company_identity,
    mock_sec_company_facts,
    mock_market_snapshot,
    sample_sentiment_docs,
):
    """Verify full 3-pillar synthesis into a `StockEvaluationReport`."""
    financials = parse_company_financials(
        company=mock_company_identity,
        company_facts=mock_sec_company_facts,
        market_snapshot=mock_market_snapshot,
    )
    scorer = SentimentScorer(use_finbert=False)
    sentiment = scorer.score_documents("AAPL", sample_sentiment_docs)

    valuation = compute_valuation_breakdown(financials)
    assert valuation.current_price == 200.0
    assert valuation.estimated_fair_value is not None and valuation.estimated_fair_value > 0
    assert valuation.pe_ratio is not None

    report = evaluate_company(financials, sentiment=sentiment)
    assert report.ticker == "AAPL"
    assert 0.0 <= report.composite_score <= 100.0
    assert report.verdict in list(InvestmentVerdict)
    assert len(report.bull_case) >= 1
    assert "Apple Inc. (AAPL)" in report.thesis_summary

