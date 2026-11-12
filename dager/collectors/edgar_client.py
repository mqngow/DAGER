"""SEC EDGAR SEC API wrapper (rate-limiting, custom User-Agent)."""

from typing import Any, Dict, Optional
import time
import httpx

from dager.config import settings


class EdgarClient:
    BASE_URL = "https://data.sec.gov"
    TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"

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
        self.ticker_to_cik: Dict[str, str] = {}

    def _throttle(self) -> None:
        elapsed = time.time() - self.last_request_time
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self.last_request_time = time.time()

    def fetch_ticker_mapping(self) -> Dict[str, str]:
        """Maps uppercase ticker symbols to 10-digit zero-padded CIK strings."""
        self._throttle()
        headers = {"User-Agent": self.user_agent}
        with httpx.Client(headers=headers, timeout=10.0) as client:
            resp = client.get(self.TICKER_MAP_URL)
            resp.raise_for_status()
            data = resp.json()
            # company_tickers.json format: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}
            self.ticker_to_cik = {
                item["ticker"].upper(): str(item["cik_str"]).zfill(10)
                for item in data.values()
            }
        return self.ticker_to_cik

    def get_cik(self, ticker: str) -> str:
        if not self.ticker_to_cik:
            self.fetch_ticker_mapping()
        ticker_clean = ticker.strip().upper()
        if ticker_clean not in self.ticker_to_cik:
            raise KeyError(f"Ticker '{ticker_clean}' not found in SEC directory.")
        return self.ticker_to_cik[ticker_clean]

    def get_company_facts(self, ticker: str) -> Dict[str, Any]:
        """Fetches all XBRL disclosures and financial facts for a given ticker."""
        cik = self.get_cik(ticker)
        url = f"{self.BASE_URL}/api/xbrl/companyfacts/CIK{cik}.json"
        
        self._throttle()
        with httpx.Client(headers=self.headers, timeout=15.0) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.json()

    def get_submissions(self, ticker: str) -> Dict[str, Any]:
        """Fetches recent filing metadata (accession numbers, forms, filing dates)."""
        cik = self.get_cik(ticker)
        url = f"{self.BASE_URL}/submissions/CIK{cik}.json"
        
        self._throttle()
        with httpx.Client(headers=self.headers, timeout=15.0) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.json()