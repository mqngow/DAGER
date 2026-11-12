"""Prefect tasks for news retrieval, RSS parsing, SEC narrative scraping, and FinBERT scoring."""

from typing import List, Optional
from sqlalchemy.engine import Engine

from dager.collectors import NewsClient, TranscriptClient
from dager.core.sentiment import SentimentScorer
from dager.pipeline import prefect_task
from dager.schemas.filings import CompanyIdentity
from dager.schemas.sentiment import CompanySentimentSummary, SentimentDocument
from dager.storage import CompanyRepo, FilingRepo, init_db, session_scope


# ==============================================================================
# 1. News, RSS & Transcript Collection Task
# Gathers recent headlines/articles from `NewsClient` (Finnhub, NewsAPI, RSS)
# and management narratives from `TranscriptClient` (SEC 8-K/10-K/10-Q & calls).
# ==============================================================================
@prefect_task(name="collect-sentiment-documents", retries=2, retry_delay_seconds=2)
def collect_sentiment_documents_task(
    company: CompanyIdentity,
    news_limit: int = 20,
    include_transcripts: bool = True,
    news_client: Optional[NewsClient] = None,
    transcript_client: Optional[TranscriptClient] = None,
) -> List[SentimentDocument]:
    """Fetch recent news articles, RSS items, and filing/earnings narratives for a company."""
    news = news_client or NewsClient()
    docs: List[SentimentDocument] = news.get_company_news(
        ticker=company.ticker,
        company_name=company.company_name,
        limit=news_limit,
    )

    if include_transcripts:
        transcripts = transcript_client or TranscriptClient()
        docs.extend(transcripts.get_company_transcripts(company.ticker, max_items=3))

    return docs


# ==============================================================================
# 2. FinBERT / Lexicon Scoring & Time-Decay Aggregation Task
# Scores collected documents and computes the recency-weighted Pillar 3 signal.
# ==============================================================================
@prefect_task(name="score-company-sentiment")
def score_company_sentiment_task(
    ticker: str,
    documents: List[SentimentDocument],
    use_finbert: bool = True,
    scorer: Optional[SentimentScorer] = None,
) -> CompanySentimentSummary:
    """Score `SentimentDocument` items with FinBERT (or lexicon fallback) and time-decay weighting."""
    engine = scorer or SentimentScorer(use_finbert=use_finbert)
    return engine.score_documents(ticker=ticker, documents=documents)


# ==============================================================================
# 3. Sentiment Persistence Task
# Stores the aggregated `CompanySentimentSummary` in the `sentiment_records` table.
# ==============================================================================
@prefect_task(name="persist-company-sentiment")
def persist_sentiment_task(
    company: CompanyIdentity,
    summary: CompanySentimentSummary,
    engine: Optional[Engine] = None,
) -> int:
    """Persist `CompanySentimentSummary` linked to `company` and return the record ID."""
    init_db(engine)
    with session_scope(engine) as session:
        company_row = CompanyRepo(session).upsert(company)
        record = FilingRepo(session).save_sentiment(company_row.id, summary)
        return record.id

