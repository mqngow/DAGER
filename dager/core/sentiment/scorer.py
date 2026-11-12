"""FinBERT sentiment weighting, exponential recency decay, and signal aggregation."""

from datetime import datetime, timezone
import math
import re
from typing import Callable, Dict, List, Optional

from dager.config import settings
from dager.core.sentiment.parser import chunk_text_for_finbert, clean_html_text
from dager.schemas.sentiment import (
    CompanySentimentSummary,
    SentimentDocument,
    SentimentLabel,
)


# ==============================================================================
# Financial Lexicon Fallback Dictionaries
# Used when `transformers`/`torch` is not installed or when running lightweight
# unit tests without downloading the 440MB HuggingFace FinBERT weights.
# ==============================================================================
BULLISH_TERMS = {
    "beat", "beats", "exceeded", "surge", "surges", "growth", "record", "upgrade",
    "upgraded", "outperform", "strong", "bullish", "profit", "profitable", "expansion",
    "dividend", "buyback", "innovation", "upside", "accelerating", "resilient", "gain",
}
BEARISH_TERMS = {
    "miss", "missed", "decline", "declines", "drop", "plunges", "downgrade", "downgraded",
    "underperform", "weak", "bearish", "loss", "losses", "layoffs", "investigation",
    "lawsuit", "default", "bankruptcy", "headwinds", "deficit", "downside", "warning",
}


# ==============================================================================
# FinBERT & Lexicon Sentiment Scorer
# Scores financial headlines, articles, and SEC MD&A chunks into `(positive,
# neutral, negative)` probabilities, applies exponential time-decay weighting,
# and produces a `CompanySentimentSummary` with a 0–100 Pillar 3 score.
# ==============================================================================
class SentimentScorer:
    def __init__(
        self,
        model_name: Optional[str] = None,
        half_life_days: Optional[float] = None,
        use_finbert: bool = True,
    ):
        self.model_name = model_name or settings.finbert_model_name
        self.half_life_days = half_life_days or settings.sentiment_decay_half_life_days
        self.use_finbert = use_finbert
        self._pipeline: Optional[Callable] = None
        self._pipeline_attempted = False

    # ==========================================================================
    # Lazy Model Loader & Inference Backend
    # Attempts to load `ProsusAI/finbert` once on first score call; gracefully
    # falls back to deterministic financial lexicon scoring if unavailable.
    # ==========================================================================
    def _get_finbert_pipeline(self) -> Optional[Callable]:
        """Lazily initialize the HuggingFace FinBERT text-classification pipeline."""
        if not self.use_finbert:
            return None
        if not self._pipeline_attempted:
            self._pipeline_attempted = True
            try:
                from transformers import pipeline  # type: ignore

                self._pipeline = pipeline(
                    "text-classification",
                    model=self.model_name,
                    top_k=None,
                    truncation=True,
                    max_length=512,
                )
            except Exception:
                self._pipeline = None
        return self._pipeline

    @staticmethod
    def _score_with_lexicon(text: str) -> Dict[str, float]:
        """Score text polarity using curated financial domain keywords."""
        tokens = re.findall(r"[a-z]+", text.lower())
        if not tokens:
            return {"positive": 0.15, "neutral": 0.70, "negative": 0.15}

        pos_hits = sum(1 for t in tokens if t in BULLISH_TERMS)
        neg_hits = sum(1 for t in tokens if t in BEARISH_TERMS)
        total_hits = pos_hits + neg_hits

        if total_hits == 0:
            return {"positive": 0.20, "neutral": 0.60, "negative": 0.20}

        raw_pos = (pos_hits + 0.5) / (total_hits + 2.0)
        raw_neg = (neg_hits + 0.5) / (total_hits + 2.0)
        neutral = max(0.05, 1.0 - (raw_pos + raw_neg))
        norm = raw_pos + raw_neg + neutral
        return {
            "positive": round(raw_pos / norm, 4),
            "neutral": round(neutral / norm, 4),
            "negative": round(raw_neg / norm, 4),
        }

    def score_text(self, text: str) -> Dict[str, float]:
        """Return `{'positive': float, 'neutral': float, 'negative': float}` probabilities for a passage."""
        chunks = chunk_text_for_finbert(text)
        if not chunks:
            return {"positive": 0.20, "neutral": 0.60, "negative": 0.20}

        nlp = self._get_finbert_pipeline()
        if nlp is not None:
            try:
                outputs = nlp(chunks)
                pos_sum = neu_sum = neg_sum = 0.0
                for chunk_res in outputs:
                    for item in chunk_res:
                        lbl = str(item["label"]).lower()
                        sc = float(item["score"])
                        if "pos" in lbl:
                            pos_sum += sc
                        elif "neg" in lbl:
                            neg_sum += sc
                        else:
                            neu_sum += sc
                n = len(outputs)
                return {
                    "positive": round(pos_sum / n, 4),
                    "neutral": round(neu_sum / n, 4),
                    "negative": round(neg_sum / n, 4),
                }
            except Exception:
                pass

        return self._score_with_lexicon(" ".join(chunks))

    # ==========================================================================
    # Exponential Time-Decay & Document Aggregation
    # Weights newer articles more heavily (`w = 0.5 ** (age_days / half_life)`)
    # and aggregates all documents into a `CompanySentimentSummary`.
    # ==========================================================================
    def compute_recency_weight(
        self,
        published_at: datetime,
        reference_time: Optional[datetime] = None,
    ) -> float:
        """Compute exponential decay weight in (0.0, 1.0] based on document age in days."""
        ref = reference_time or datetime.now(timezone.utc)
        pub = (
            published_at.astimezone(timezone.utc)
            if published_at.tzinfo
            else published_at.replace(tzinfo=timezone.utc)
        )
        age_days = max(0.0, (ref - pub).total_seconds() / 86400.0)
        weight = math.pow(0.5, age_days / self.half_life_days)
        return round(max(0.05, min(1.0, weight)), 4)

    def score_documents(
        self,
        ticker: str,
        documents: List[SentimentDocument],
        reference_time: Optional[datetime] = None,
    ) -> CompanySentimentSummary:
        """Score a list of `SentimentDocument` items and aggregate into `CompanySentimentSummary`."""
        symbol = ticker.strip().upper()
        if not documents:
            return CompanySentimentSummary(
                ticker=symbol,
                document_count=0,
                weighted_polarity=0.0,
                sentiment_score_0_100=50.0,
                overall_label=SentimentLabel.NEUTRAL,
            )

        scored_docs: List[SentimentDocument] = []
        weighted_polarity_sum = 0.0
        weight_sum = 0.0

        for doc in documents:
            combined_text = f"{doc.title}. {clean_html_text(doc.content or '')}".strip()
            probs = self.score_text(combined_text)
            weight = self.compute_recency_weight(doc.published_at, reference_time=reference_time)

            # Determine dominant label
            dominant = max(probs.items(), key=lambda kv: kv[1])[0]
            label_enum = SentimentLabel(dominant)

            scored_doc = doc.model_copy(
                update={
                    "label": label_enum,
                    "positive_prob": probs["positive"],
                    "neutral_prob": probs["neutral"],
                    "negative_prob": probs["negative"],
                    "recency_weight": weight,
                }
            )
            scored_docs.append(scored_doc)

            weighted_polarity_sum += scored_doc.net_polarity * weight
            weight_sum += weight

        weighted_polarity = round(weighted_polarity_sum / weight_sum, 4) if weight_sum > 0 else 0.0
        # Map [-1.0, +1.0] polarity into [0, 100] score centered at 50
        sentiment_score = round(max(0.0, min(100.0, 50.0 + weighted_polarity * 50.0)), 2)

        if weighted_polarity >= 0.12:
            overall_label = SentimentLabel.POSITIVE
        elif weighted_polarity <= -0.12:
            overall_label = SentimentLabel.NEGATIVE
        else:
            overall_label = SentimentLabel.NEUTRAL

        # Extract top bullish and bearish headlines for the investment thesis
        bullish_sorted = sorted(
            [d for d in scored_docs if d.net_polarity > 0.05],
            key=lambda d: d.net_polarity * d.recency_weight,
            reverse=True,
        )
        bearish_sorted = sorted(
            [d for d in scored_docs if d.net_polarity < -0.05],
            key=lambda d: abs(d.net_polarity) * d.recency_weight,
            reverse=True,
        )

        return CompanySentimentSummary(
            ticker=symbol,
            evaluated_at=reference_time or datetime.now(timezone.utc),
            document_count=len(scored_docs),
            weighted_polarity=weighted_polarity,
            sentiment_score_0_100=sentiment_score,
            overall_label=overall_label,
            top_bullish_headlines=[d.title for d in bullish_sorted[:3]],
            top_bearish_headlines=[d.title for d in bearish_sorted[:3]],
            documents=scored_docs,
        )

