"""SEC EDGAR API wrapper (CIK lookup, fuzzy company name resolver, S&P 500 loader, and XBRL fetcher)."""

from difflib import SequenceMatcher
import re
import time
from typing import Any, Dict, List, Optional
from bs4 import BeautifulSoup
import httpx

from dager.config import settings
from dager.schemas.filings import CompanyIdentity


# ==============================================================================
# Fallback Core S&P 500 Tickers
# Used as a resilient fallback if external Wikipedia scraping is unreachable.
# ==============================================================================
DEFAULT_SP500_SAMPLE: List[str] = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "BRK-B", "LLY", "AVGO", "JPM",
    "TSLA", "UNH", "XOM", "V", "PG", "MA", "COST", "JNJ", "HD", "MRK",
]


# ==============================================================================
# SEC EDGAR Client
# Handles rate-limited communication with data.sec.gov, ticker/title resolution
# via SEC's company_tickers.json, S&P 500 universe loading, and XBRL extraction.
# ==============================================================================
class EdgarClient:
    BASE_URL = "https://data.sec.gov"
    TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
    WIKIPEDIA_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

    # Class-level shared directory caches so `EdgarClient` instances stay lightweight
    # for Prefect parameter serialization (<512KB limit) and share SEC mappings across runs.
    _SHARED_TICKER_TO_CIK: Dict[str, str] = {}
    _SHARED_DIRECTORY: Dict[str, Dict[str, str]] = {}
    _SHARED_SP500_TICKERS: set[str] = set(DEFAULT_SP500_SAMPLE)

    def __init__(
        self,
        user_agent: Optional[str] = None,
        min_interval_seconds: Optional[float] = None,  # Caps requests at ~6.6 req/sec by default
    ):
        # Fall back to centralized config settings if explicit arguments are omitted
        self.user_agent = user_agent or settings.sec_user_agent
        if not self.user_agent:
            raise ValueError(
                "A descriptive SEC_USER_AGENT ('AppName ContactEmail') must be specified."
            )

        self.min_interval = (
            min_interval_seconds
            if min_interval_seconds is not None
            else settings.sec_min_interval_seconds
        )
        self.last_request_time = 0.0
        self.headers = {
            "User-Agent": self.user_agent,
            "Accept-Encoding": "gzip, deflate",
            "Host": "data.sec.gov",
        }

    @property
    def ticker_to_cik(self) -> Dict[str, str]:
        """Shared ticker-to-CIK mapping across EdgarClient instances."""
        return EdgarClient._SHARED_TICKER_TO_CIK

    @ticker_to_cik.setter
    def ticker_to_cik(self, value: Dict[str, str]) -> None:
        EdgarClient._SHARED_TICKER_TO_CIK = value

    @property
    def directory(self) -> Dict[str, Dict[str, str]]:
        """Shared SEC registrant directory across EdgarClient instances."""
        return EdgarClient._SHARED_DIRECTORY

    @directory.setter
    def directory(self, value: Dict[str, Dict[str, str]]) -> None:
        EdgarClient._SHARED_DIRECTORY = value

    @property
    def sp500_tickers(self) -> set[str]:
        """Shared S&P 500 ticker set across EdgarClient instances."""
        return EdgarClient._SHARED_SP500_TICKERS

    @sp500_tickers.setter
    def sp500_tickers(self, value: set[str]) -> None:
        EdgarClient._SHARED_SP500_TICKERS = value

    # ==========================================================================
    # Rate Limiting & Directory Loading
    # Ensures compliance with the SEC EDGAR 10 requests/second maximum limit
    # and caches the full SEC registrant directory (ticker, CIK, and title).
    # ==========================================================================
    def _throttle(self) -> None:
        """Sleep if necessary to enforce minimum spacing between SEC HTTP requests."""
        elapsed = time.time() - self.last_request_time
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self.last_request_time = time.time()

    def fetch_ticker_mapping(self) -> Dict[str, str]:
        """Download and cache SEC's `company_tickers.json` mapping tickers and company titles to 10-digit CIKs."""
        self._throttle()
        headers = {"User-Agent": self.user_agent}
        with httpx.Client(headers=headers, timeout=15.0) as client:
            resp = client.get(self.TICKER_MAP_URL)
            resp.raise_for_status()
            data = resp.json()

            # company_tickers.json format: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}
            self.ticker_to_cik = {}
            self.directory = {}
            for item in data.values():
                ticker = str(item["ticker"]).upper().strip()
                cik = str(item["cik_str"]).zfill(10)
                title = str(item.get("title", "")).strip()
                self.ticker_to_cik[ticker] = cik
                self.directory[ticker] = {
                    "ticker": ticker,
                    "cik": cik,
                    "title": title,
                }
        return self.ticker_to_cik

    # ==========================================================================
    # Company Name & Ticker Resolver
    # Resolves either an exact ticker symbol (e.g., "AAPL", "BRK.B") or a free-text
    # company name query (e.g., "Apple", "Microsoft Corp") to a `CompanyIdentity`.
    # ==========================================================================
    @staticmethod
    def _normalize_name(text: str) -> str:
        """Strip corporate suffixes and punctuation to improve fuzzy company name matching."""
        cleaned = re.sub(r"[^\w\s]", " ", text.lower())
        suffixes = {"inc", "corp", "corporation", "co", "company", "ltd", "plc", "holdings", "group", "the"}
        tokens = [tok for tok in cleaned.split() if tok not in suffixes]
        return " ".join(tokens) if tokens else cleaned.strip()

    def resolve_company(self, query: str) -> CompanyIdentity:
        """Resolve a ticker symbol or company name (exact or fuzzy) into a validated `CompanyIdentity`."""
        if not self.directory:
            self.fetch_ticker_mapping()

        raw_query = query.strip()
        if not raw_query:
            raise ValueError("Company search query cannot be empty.")

        # 1. Check exact ticker match (normalizing '.' to '-' for SEC convention, e.g., BRK.B -> BRK-B)
        ticker_candidate = raw_query.upper().replace(".", "-")
        if ticker_candidate in self.directory:
            entry = self.directory[ticker_candidate]
            return CompanyIdentity(
                ticker=entry["ticker"],
                cik=entry["cik"],
                company_name=entry["title"],
                is_sp500=entry["ticker"] in self.sp500_tickers,
            )

        # 2. Fuzzy & substring match against SEC registrant titles
        norm_query = self._normalize_name(raw_query)
        best_ticker: Optional[str] = None
        best_score: float = 0.0

        for ticker, entry in self.directory.items():
            norm_title = self._normalize_name(entry["title"])
            if not norm_title:
                continue

            # Exact normalized title match wins immediately
            if norm_query == norm_title:
                best_ticker = ticker
                best_score = 1.0
                break

            # Boost score when query is a direct prefix or whole-word substring of the company title
            similarity = SequenceMatcher(None, norm_query, norm_title).ratio()
            if norm_title.startswith(norm_query):
                similarity = max(similarity, 0.88)
            elif norm_query in norm_title.split():
                similarity = max(similarity, 0.82)

            if similarity > best_score:
                best_score = similarity
                best_ticker = ticker

        if best_ticker and best_score >= 0.65:
            entry = self.directory[best_ticker]
            return CompanyIdentity(
                ticker=entry["ticker"],
                cik=entry["cik"],
                company_name=entry["title"],
                is_sp500=entry["ticker"] in self.sp500_tickers,
            )

        raise KeyError(f"Could not resolve '{query}' to a valid SEC ticker or company title.")

    def get_cik(self, ticker_or_query: str) -> str:
        """Return the 10-digit zero-padded CIK for a ticker or company name."""
        return self.resolve_company(ticker_or_query).cik

    # ==========================================================================
    # S&P 500 Constituent List Loader
    # Scrapes the current S&P 500 constituent table from Wikipedia and normalizes
    # tickers to SEC format (e.g., BRK.B -> BRK-B), with an offline fallback.
    # ==========================================================================
    def fetch_sp500_tickers(self) -> List[str]:
        """Fetch the current list of S&P 500 ticker symbols for daily batch ingestion."""
        try:
            headers = {"User-Agent": self.user_agent}
            with httpx.Client(headers=headers, timeout=15.0, follow_redirects=True) as client:
                resp = client.get(self.WIKIPEDIA_SP500_URL)
                resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "html.parser")
            table = soup.find("table", {"id": "constituents"})
            if not table:
                raise ValueError("S&P 500 constituents table not found on page.")

            # Exclude secondary share-class duplicates (GOOG, FOX, NWS) and cap to 500
            secondary_share_classes = {"GOOG", "FOX", "NWS"}
            tickers: List[str] = []
            for row in table.find_all("tr")[1:]:
                cells = row.find_all(["td", "th"])
                if cells:
                    symbol = cells[0].get_text(strip=True).upper().replace(".", "-")
                    if symbol and symbol not in secondary_share_classes:
                        tickers.append(symbol)

            tickers = tickers[:500]
            if tickers:
                self.sp500_tickers = set(tickers)
                return tickers
        except Exception:
            # Fall back to core sample list if offline or rate-limited
            pass

        self.sp500_tickers = set(DEFAULT_SP500_SAMPLE)
        return list(DEFAULT_SP500_SAMPLE)

    # ==========================================================================
    # XBRL Facts & Filing Submissions Endpoints
    # Queries SEC EDGAR's `companyfacts` and `submissions` JSON APIs for a given
    # ticker or company name.
    # ==========================================================================
    def get_company_facts(self, ticker_or_query: str) -> Dict[str, Any]:
        """Fetch all XBRL disclosures and financial facts (`us-gaap`) for a given ticker or company name."""
        cik = self.get_cik(ticker_or_query)
        url = f"{self.BASE_URL}/api/xbrl/companyfacts/CIK{cik}.json"

        self._throttle()
        with httpx.Client(headers=self.headers, timeout=20.0) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.json()

    def get_submissions(self, ticker_or_query: str) -> Dict[str, Any]:
        """Fetch recent filing metadata (accession numbers, form types, report dates) for a company."""
        cik = self.get_cik(ticker_or_query)
        url = f"{self.BASE_URL}/submissions/CIK{cik}.json"

        self._throttle()
        with httpx.Client(headers=self.headers, timeout=15.0) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.json()
