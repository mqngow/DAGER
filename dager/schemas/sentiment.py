"""Structured news article, transcript, and FinBERT sentiment score models."""

from datetime import datetime
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field


# ==============================================================================
# Sentiment Source & Polarity Enumerations
# Distinguishes between news headlines, SEC MD&A narrative sections, and
# earnings call transcripts, as well as FinBERT classification labels.
# ==============================================================================
class SentimentSourceType(str, Enum):
    NEWS_ARTICLE = "news_article"
    RSS_FEED = "rss_feed"
    PRESS_RELEASE = "press_release"
    SEC_MDA = "sec_mda"
    EARNINGS_TRANSCRIPT = "earnings_transcript"


class SentimentLabel(str, Enum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"


# ==============================================================================
# Raw & Scored Document Item Schema
# Represents a single news article, press release, or filing excerpt alongside
# its FinBERT probability distribution and recency weight.
# ==============================================================================
class SentimentDocument(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    # --- Source Metadata ---
    ticker: str = Field(..., description="Associated company ticker symbol.")
    source_type: SentimentSourceType = Field(..., description="Origin of the text document.")
    title: str = Field(..., description="Headline or section title.")
    content: Optional[str] = Field(default=None, description="Cleaned body text or summary snippet.")
    url: Optional[str] = Field(default=None, description="Canonical URL to the source article or filing.")
    published_at: datetime = Field(..., description="Publication timestamp used for time-decay weighting.")

    # --- FinBERT Scoring Outputs ---
    label: Optional[SentimentLabel] = Field(default=None, description="Dominant FinBERT class label.")
    positive_prob: float = Field(default=0.0, ge=0.0, le=1.0, description="FinBERT positive probability.")
    neutral_prob: float = Field(default=0.0, ge=0.0, le=1.0, description="FinBERT neutral probability.")
    negative_prob: float = Field(default=0.0, ge=0.0, le=1.0, description="FinBERT negative probability.")
    recency_weight: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Exponential time-decay weight (1.0 = published today, decays with age).",
    )

    @property
    def net_polarity(self) -> float:
        """Continuous sentiment polarity in [-1.0, 1.0] computed as (P(pos) - P(neg))."""
        return self.positive_prob - self.negative_prob


# ==============================================================================
# Aggregated Company Sentiment Summary Schema
# Synthesizes all scored documents for a ticker into a single time-decay-weighted
# qualitative signal consumed by the 6-month to 3-year Evaluator.
# ==============================================================================
class CompanySentimentSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ticker: str = Field(..., description="Company stock ticker.")
    evaluated_at: datetime = Field(default_factory=datetime.utcnow, description="Aggregation timestamp.")
    document_count: int = Field(default=0, ge=0, description="Total number of documents analyzed.")

    # --- Weighted Sentiment Metrics ---
    weighted_polarity: float = Field(
        default=0.0,
        ge=-1.0,
        le=1.0,
        description="Recency-weighted net sentiment polarity in [-1.0, +1.0].",
    )
    sentiment_score_0_100: float = Field(
        default=50.0,
        ge=0.0,
        le=100.0,
        description="Normalized sentiment pillar score (0 = strongly negative, 50 = neutral, 100 = strongly positive).",
    )
    overall_label: SentimentLabel = Field(
        default=SentimentLabel.NEUTRAL,
        description="High-level qualitative sentiment classification.",
    )

    # --- Narrative Highlights ---
    top_bullish_headlines: List[str] = Field(
        default_factory=list,
        description="Highest-weighted positive headlines or transcript takeaways.",
    )
    top_bearish_headlines: List[str] = Field(
        default_factory=list,
        description="Highest-weighted negative headlines or risk factors.",
    )
    documents: List[SentimentDocument] = Field(
        default_factory=list,
        description="Underlying scored articles and excerpts.",
    )

