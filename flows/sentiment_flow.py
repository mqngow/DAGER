"""Prefect flow for financial news, RSS, and transcript sentiment scoring (`sentiment_flow.py`)."""

from typing import List, Optional
from sqlalchemy.engine import Engine

from dager.collectors import EdgarClient, NewsClient, TranscriptClient
from dager.core.sentiment import SentimentScorer
from dager.pipeline import (
    collect_sentiment_documents_task,
    fetch_sp500_universe_task,
    persist_sentiment_task,
    prefect_flow,
    resolve_company_task,
    score_company_sentiment_task,
)
from dager.schemas.filings import CompanyIdentity
from dager.schemas.sentiment import CompanySentimentSummary


# ==============================================================================
# 1. Single-Company Sentiment Ingestion & Scoring Flow
# Pulls recent company news, RSS headlines, and SEC 8-K/10-K/10-Q narratives,
# scores them with FinBERT (or lexicon fallback) + recency decay, and saves to DB.
# ==============================================================================
@prefect_flow(name="ingest-company-sentiment")
def ingest_company_sentiment_flow(
    query_or_identity: str | CompanyIdentity,
    include_transcripts: bool = True,
    use_finbert: bool = True,
    persist: bool = True,
    engine: Optional[Engine] = None,
    news_client: Optional[NewsClient] = None,
    transcript_client: Optional[TranscriptClient] = None,
    scorer: Optional[SentimentScorer] = None,
) -> CompanySentimentSummary:
    """Collect and score news/filing sentiment for a company and optionally persist to DB."""
    identity = (
        query_or_identity
        if isinstance(query_or_identity, CompanyIdentity)
        else resolve_company_task(query_or_identity)
    )
    docs = collect_sentiment_documents_task(
        company=identity,
        include_transcripts=include_transcripts,
        news_client=news_client,
        transcript_client=transcript_client,
    )
    summary = score_company_sentiment_task(
        ticker=identity.ticker,
        documents=docs,
        use_finbert=use_finbert,
        scorer=scorer,
    )
    if persist:
        persist_sentiment_task(identity, summary, engine=engine)
    return summary


# ==============================================================================
# 2. Batch S&P 500 Sentiment Flow
# Periodically refreshes news and filing sentiment across S&P 500 constituents.
# ==============================================================================
@prefect_flow(name="batch-sp500-sentiment")
def batch_sp500_sentiment_flow(
    tickers: Optional[List[str]] = None,
    limit: Optional[int] = None,
    use_finbert: bool = True,
    engine: Optional[Engine] = None,
) -> List[CompanySentimentSummary]:
    """Batch-collect and score news/transcript sentiment across the S&P 500 universe."""
    edgar = EdgarClient()
    news = NewsClient()
    transcripts = TranscriptClient(edgar_client=edgar)
    scorer = SentimentScorer(use_finbert=use_finbert)

    universe = tickers or fetch_sp500_universe_task(limit=limit, edgar_client=edgar)
    summaries: List[CompanySentimentSummary] = []

    for symbol in universe:
        try:
            identity = resolve_company_task(symbol, edgar_client=edgar)
            summary = ingest_company_sentiment_flow(
                query_or_identity=identity,
                use_finbert=use_finbert,
                persist=True,
                engine=engine,
                news_client=news,
                transcript_client=transcripts,
                scorer=scorer,
            )
            summaries.append(summary)
        except Exception:
            continue
    return summaries

