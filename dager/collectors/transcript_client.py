"""Earnings call transcript and SEC 8-K / 10-K / 10-Q management commentary collector."""

from datetime import datetime, timezone
import re
from typing import List, Optional
from bs4 import BeautifulSoup
import httpx

from dager.collectors.edgar_client import EdgarClient
from dager.config import settings
from dager.schemas.sentiment import SentimentDocument, SentimentSourceType


# ==============================================================================
# Earnings Transcript & Filing Narrative Client
# Extracts qualitative management commentary from two sources:
# 1. Free SEC EDGAR 8-K (Item 2.02 Earnings Releases / Item 7.01 Reg FD) and
#    10-K/10-Q primary filing documents (zero API key required).
# 2. Optional Finnhub earnings call transcript API if a key is available.
# ==============================================================================
class TranscriptClient:
    SEC_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
    FINNHUB_TRANSCRIPTS_URL = "https://finnhub.io/api/v1/stock/transcripts"

    def __init__(
        self,
        edgar_client: Optional[EdgarClient] = None,
        finnhub_api_key: Optional[str] = None,
    ):
        self.edgar = edgar_client or EdgarClient()
        self.finnhub_api_key = finnhub_api_key or settings.finnhub_api_key

    # ==========================================================================
    # HTML Cleaning & Narrative Excerpt Helper
    # Strips XBRL/HTML markup and extracts clean prose paragraphs suitable for
    # FinBERT token windows.
    # ==========================================================================
    @staticmethod
    def _clean_filing_html(raw_html: str, max_chars: int = 4000) -> str:
        """Strip HTML/iXBRL tags and return a clean text excerpt for NLP scoring."""
        soup = BeautifulSoup(raw_html, "html.parser")
        for tag in soup(["script", "style", "table", "ix:header"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]

    # ==========================================================================
    # Free SEC EDGAR 8-K Earnings Release & 10-Q/10-K Narrative Fetcher
    # Uses the company's SEC `submissions` history to locate recent 8-K earnings
    # releases and periodic reports, downloading their primary HTML narrative.
    # ==========================================================================
    def fetch_sec_earnings_and_mda(
        self,
        ticker: str,
        max_filings: int = 3,
    ) -> List[SentimentDocument]:
        """Fetch recent 8-K earnings disclosures and 10-K/10-Q narrative excerpts from SEC EDGAR."""
        symbol = ticker.strip().upper()
        try:
            submissions = self.edgar.get_submissions(symbol)
            cik_int = int(self.edgar.get_cik(symbol))
        except Exception:
            return []

        recent = submissions.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        accessions = recent.get("accessionNumber", [])
        filing_dates = recent.get("filingDate", [])
        primary_docs = recent.get("primaryDocument", [])

        docs: List[SentimentDocument] = []
        headers = {"User-Agent": self.edgar.user_agent}

        for form, acc, f_date, prim_doc in zip(forms, accessions, filing_dates, primary_docs):
            if form not in ("8-K", "10-Q", "10-K"):
                continue
            if not acc or not prim_doc:
                continue

            acc_no_hyphens = str(acc).replace("-", "")
            doc_url = f"{self.SEC_ARCHIVES_BASE}/{cik_int}/{acc_no_hyphens}/{prim_doc}"

            try:
                self.edgar._throttle()
                with httpx.Client(headers=headers, timeout=15.0, follow_redirects=True) as client:
                    resp = client.get(doc_url)
                    resp.raise_for_status()
                excerpt = self._clean_filing_html(resp.text)
                if not excerpt:
                    continue

                pub_dt = datetime.strptime(f_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                source_type = (
                    SentimentSourceType.PRESS_RELEASE
                    if form == "8-K"
                    else SentimentSourceType.SEC_MDA
                )
                docs.append(
                    SentimentDocument(
                        ticker=symbol,
                        source_type=source_type,
                        title=f"{symbol} Form {form} Filing Narrative ({f_date})",
                        content=excerpt,
                        url=doc_url,
                        published_at=pub_dt,
                    )
                )
                if len(docs) >= max_filings:
                    break
            except Exception:
                continue

        return docs

    # ==========================================================================
    # Optional Finnhub Earnings Call Transcript Fetcher
    # Queries Finnhub's `/stock/transcripts` endpoint if configured.
    # ==========================================================================
    def fetch_finnhub_transcripts(
        self,
        ticker: str,
        limit: int = 2,
    ) -> List[SentimentDocument]:
        """Fetch earnings call transcripts from Finnhub if an API key with transcript access is present."""
        if not self.finnhub_api_key:
            return []

        symbol = ticker.strip().upper()
        docs: List[SentimentDocument] = []

        try:
            with httpx.Client(timeout=10.0) as client:
                list_resp = client.get(
                    f"{self.FINNHUB_TRANSCRIPTS_URL}/list",
                    params={"symbol": symbol, "token": self.finnhub_api_key},
                )
                list_resp.raise_for_status()
                transcript_meta = list_resp.json().get("transcripts", [])[:limit]

                for meta in transcript_meta:
                    t_id = meta.get("id")
                    if not t_id:
                        continue
                    detail_resp = client.get(
                        self.FINNHUB_TRANSCRIPTS_URL,
                        params={"id": t_id, "token": self.finnhub_api_key},
                    )
                    detail_resp.raise_for_status()
                    detail = detail_resp.json()

                    speeches = [
                        str(turn.get("speech", [""])[0])
                        for turn in detail.get("transcript", [])
                        if turn.get("speech")
                    ]
                    content = " ".join(speeches).strip()[:4000]
                    if not content:
                        continue

                    raw_time = str(meta.get("time", ""))[:10]
                    try:
                        pub_dt = datetime.strptime(raw_time, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    except Exception:
                        pub_dt = datetime.now(timezone.utc)

                    docs.append(
                        SentimentDocument(
                            ticker=symbol,
                            source_type=SentimentSourceType.EARNINGS_TRANSCRIPT,
                            title=str(meta.get("title") or f"{symbol} Earnings Call Transcript"),
                            content=content,
                            url=None,
                            published_at=pub_dt,
                        )
                    )
        except Exception:
            return []

        return docs

    # ==========================================================================
    # Unified Transcript & Management Commentary Entrypoint
    # Returns both earnings transcripts (when available) and SEC 8-K/10-K/10-Q
    # narrative disclosures.
    # ==========================================================================
    def get_company_transcripts(
        self,
        ticker: str,
        max_items: int = 4,
    ) -> List[SentimentDocument]:
        """Retrieve earnings call transcripts and SEC filing narratives for a ticker."""
        results: List[SentimentDocument] = []
        results.extend(self.fetch_finnhub_transcripts(ticker, limit=2))
        results.extend(self.fetch_sec_earnings_and_mda(ticker, max_filings=max_items))
        return results[:max_items]

