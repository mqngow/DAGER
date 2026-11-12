"""Financial news, RSS, and press-release scrapers."""

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import List, Optional
from urllib.parse import quote_plus
import feedparser
import httpx

from dager.config import settings
from dager.schemas.sentiment import SentimentDocument, SentimentSourceType


# ==============================================================================
# Financial News & RSS Client
# Collects recent company news from free, keyless RSS feeds (Yahoo Finance RSS &
# Google News RSS) out of the box, and automatically augments with Finnhub and
# NewsAPI.org when free API keys are present in `.env`.
# ==============================================================================
class NewsClient:
    YAHOO_RSS_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
    GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search?q={query}+stock+when:14d&hl=en-US&gl=US&ceid=US:en"
    FINNHUB_NEWS_URL = "https://finnhub.io/api/v1/company-news"
    NEWSAPI_URL = "https://newsapi.org/v2/everything"

    def __init__(
        self,
        finnhub_api_key: Optional[str] = None,
        news_api_key: Optional[str] = None,
        user_agent: Optional[str] = None,
    ):
        self.finnhub_api_key = finnhub_api_key or settings.finnhub_api_key
        self.news_api_key = news_api_key or settings.news_api_key
        self.user_agent = user_agent or settings.sec_user_agent

    # ==========================================================================
    # Timestamp Parsing & Deduplication Helpers
    # Normalizes RFC-2822 RSS dates, ISO-8601 strings, and Unix epochs into UTC
    # datetimes and deduplicates headlines across providers.
    # ==========================================================================
    @staticmethod
    def _parse_rss_date(raw_date: Optional[str]) -> datetime:
        """Parse RSS `published` date string into a timezone-aware UTC datetime."""
        if not raw_date:
            return datetime.now(timezone.utc)
        try:
            dt = parsedate_to_datetime(raw_date)
            return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return datetime.now(timezone.utc)

    # ==========================================================================
    # Free RSS Feed Collectors (No API Key Required)
    # Pulls headlines and article summaries from Yahoo Finance and Google News.
    # ==========================================================================
    def fetch_rss_news(
        self,
        ticker: str,
        company_name: Optional[str] = None,
        limit: int = 20,
    ) -> List[SentimentDocument]:
        """Fetch recent company news from Yahoo Finance and Google News RSS feeds."""
        symbol = ticker.strip().upper()
        search_term = quote_plus(f"{symbol} {company_name}" if company_name else symbol)
        feed_urls = [
            self.YAHOO_RSS_URL.format(ticker=quote_plus(symbol)),
            self.GOOGLE_NEWS_RSS_URL.format(query=search_term),
        ]

        docs: List[SentimentDocument] = []
        headers = {"User-Agent": self.user_agent}

        for url in feed_urls:
            try:
                with httpx.Client(headers=headers, timeout=10.0, follow_redirects=True) as client:
                    resp = client.get(url)
                    resp.raise_for_status()
                parsed = feedparser.parse(resp.text)
                for entry in parsed.entries:
                    title = str(entry.get("title", "")).strip()
                    if not title:
                        continue
                    summary = str(entry.get("summary", "") or entry.get("description", "")).strip() or None
                    link = str(entry.get("link", "")).strip() or None
                    pub_dt = self._parse_rss_date(entry.get("published") or entry.get("updated"))

                    docs.append(
                        SentimentDocument(
                            ticker=symbol,
                            source_type=SentimentSourceType.RSS_FEED,
                            title=title,
                            content=summary,
                            url=link,
                            published_at=pub_dt,
                        )
                    )
            except Exception:
                continue

        return docs[:limit]

    # ==========================================================================
    # Finnhub Company News Collector (Optional Free API Key)
    # Queries Finnhub's `/company-news` endpoint over a lookback window when
    # `FINNHUB_API_KEY` is configured.
    # ==========================================================================
    def fetch_finnhub_news(
        self,
        ticker: str,
        lookback_days: int = 14,
        limit: int = 20,
    ) -> List[SentimentDocument]:
        """Fetch curated company news articles from Finnhub if an API key is configured."""
        if not self.finnhub_api_key:
            return []

        symbol = ticker.strip().upper()
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=lookback_days)

        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(
                    self.FINNHUB_NEWS_URL,
                    params={
                        "symbol": symbol,
                        "from": start_dt.strftime("%Y-%m-%d"),
                        "to": end_dt.strftime("%Y-%m-%d"),
                        "token": self.finnhub_api_key,
                    },
                )
                resp.raise_for_status()
                items = resp.json()
        except Exception:
            return []

        docs: List[SentimentDocument] = []
        for item in items[:limit]:
            headline = str(item.get("headline", "")).strip()
            if not headline:
                continue
            epoch = item.get("datetime")
            pub_dt = (
                datetime.fromtimestamp(int(epoch), tz=timezone.utc)
                if epoch
                else datetime.now(timezone.utc)
            )
            docs.append(
                SentimentDocument(
                    ticker=symbol,
                    source_type=SentimentSourceType.NEWS_ARTICLE,
                    title=headline,
                    content=str(item.get("summary", "")).strip() or None,
                    url=str(item.get("url", "")).strip() or None,
                    published_at=pub_dt,
                )
            )
        return docs

    # ==========================================================================
    # NewsAPI.org Collector (Optional Free Developer API Key)
    # Queries NewsAPI `/v2/everything` when `NEWS_API_KEY` is configured.
    # ==========================================================================
    def fetch_newsapi_articles(
        self,
        ticker: str,
        company_name: Optional[str] = None,
        limit: int = 15,
    ) -> List[SentimentDocument]:
        """Fetch financial news articles from NewsAPI.org if `NEWS_API_KEY` is configured."""
        if not self.news_api_key:
            return []

        symbol = ticker.strip().upper()
        query = f'"{company_name}" OR "{symbol}"' if company_name else symbol

        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(
                    self.NEWSAPI_URL,
                    params={
                        "q": query,
                        "language": "en",
                        "sortBy": "publishedAt",
                        "pageSize": limit,
                        "apiKey": self.news_api_key,
                    },
                )
                resp.raise_for_status()
                articles = resp.json().get("articles", [])
        except Exception:
            return []

        docs: List[SentimentDocument] = []
        for art in articles[:limit]:
            title = str(art.get("title", "")).strip()
            if not title or title == "[Removed]":
                continue
            raw_pub = art.get("publishedAt")
            try:
                pub_dt = datetime.fromisoformat(str(raw_pub).replace("Z", "+00:00"))
            except Exception:
                pub_dt = datetime.now(timezone.utc)

            docs.append(
                SentimentDocument(
                    ticker=symbol,
                    source_type=SentimentSourceType.NEWS_ARTICLE,
                    title=title,
                    content=str(art.get("description", "") or "").strip() or None,
                    url=str(art.get("url", "") or "").strip() or None,
                    published_at=pub_dt,
                )
            )
        return docs

    # ==========================================================================
    # Unified Company News Aggregator
    # Combines Finnhub, NewsAPI, and free RSS feeds into a single deduplicated,
    # chronologically sorted list of `SentimentDocument` objects.
    # ==========================================================================
    def get_company_news(
        self,
        ticker: str,
        company_name: Optional[str] = None,
        limit: int = 30,
    ) -> List[SentimentDocument]:
        """Aggregate and deduplicate recent news from all configured sources (newest first)."""
        combined: List[SentimentDocument] = []
        combined.extend(self.fetch_finnhub_news(ticker, limit=limit))
        combined.extend(self.fetch_newsapi_articles(ticker, company_name=company_name, limit=limit))
        combined.extend(self.fetch_rss_news(ticker, company_name=company_name, limit=limit))

        # Deduplicate by normalized lowercase headline title
        seen_titles: set[str] = set()
        unique_docs: List[SentimentDocument] = []
        for doc in combined:
            key = doc.title.lower().strip()
            if key not in seen_titles:
                seen_titles.add(key)
                unique_docs.append(doc)

        unique_docs.sort(key=lambda d: d.published_at, reverse=True)
        return unique_docs[:limit]

