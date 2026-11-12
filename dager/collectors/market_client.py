"""Historical & live stock price, market cap, and benchmark comparison client."""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import httpx
import yfinance as yf

from dager.config import settings
from dager.schemas.filings import MarketPriceSnapshot


# ==============================================================================
# Market Price & Valuation Context Client
# Uses `yfinance` (free, no API key required) as the primary provider for
# 3–5 year price histories, 52-week ranges, market cap, and S&P 500 (`^GSPC`)
# relative performance, with an optional Finnhub REST fallback.
# ==============================================================================
class MarketClient:
    BENCHMARK_SYMBOL = "^GSPC"  # S&P 500 Index
    FINNHUB_BASE_URL = "https://finnhub.io/api/v1"

    def __init__(self, finnhub_api_key: Optional[str] = None):
        self.finnhub_api_key = finnhub_api_key or settings.finnhub_api_key

    # ==========================================================================
    # Ticker Normalization & Return Calculation Helpers
    # Converts SEC hyphenated tickers (e.g., BRK-B) into Yahoo Finance format
    # and computes trailing percentage returns over 6m, 1y, and 3y windows.
    # ==========================================================================
    @staticmethod
    def _to_yf_symbol(ticker: str) -> str:
        """Normalize ticker symbol for Yahoo Finance compatibility."""
        return ticker.strip().upper().replace(".", "-")

    @staticmethod
    def _compute_period_return(closes: List[float], trading_days_back: int) -> Optional[float]:
        """Calculate simple trailing return `(P_now - P_then) / P_then` if enough history exists."""
        if not closes or len(closes) <= trading_days_back:
            return None
        start_price = float(closes[-1 - trading_days_back])
        end_price = float(closes[-1])
        if start_price <= 0:
            return None
        return round((end_price - start_price) / start_price, 4)

    # ==========================================================================
    # Primary Snapshot Fetcher (yfinance + Optional Finnhub Fallback)
    # Pulls 5 years of daily close history plus fast company info (market cap,
    # enterprise value, 52-week high/low) and returns a `MarketPriceSnapshot`.
    # ==========================================================================
    def get_price_snapshot(self, ticker: str) -> MarketPriceSnapshot:
        """Fetch current stock price, market cap, 52w range, and 6m/1y/3y returns."""
        symbol = self._to_yf_symbol(ticker)
        yf_ticker = yf.Ticker(symbol)

        # Fetch 5-year daily price history (approx. 252 trading days/year)
        hist = yf_ticker.history(period="5y", interval="1d", auto_adjust=True)
        closes: List[float] = []
        if hist is not None and not hist.empty and "Close" in hist:
            closes = [float(val) for val in hist["Close"].dropna().tolist()]

        # Extract fast metadata (market cap, 52-week bounds, current price)
        info: Dict[str, Any] = {}
        try:
            info = yf_ticker.info or {}
        except Exception:
            info = {}

        current_price = (
            info.get("currentPrice")
            or info.get("regularMarketPrice")
            or (closes[-1] if closes else None)
        )

        # Optional fallback to Finnhub quote endpoint if yfinance returned no price
        if not current_price and self.finnhub_api_key:
            current_price = self._fetch_finnhub_price(symbol)

        if not current_price or current_price <= 0:
            raise ValueError(f"Unable to retrieve valid market price for '{symbol}'.")

        # Compute 52-week high/low from history if missing from `info`
        recent_year = closes[-252:] if len(closes) >= 252 else closes
        high_52w = info.get("fiftyTwoWeekHigh") or (max(recent_year) if recent_year else None)
        low_52w = info.get("fiftyTwoWeekLow") or (min(recent_year) if recent_year else None)

        return MarketPriceSnapshot(
            ticker=symbol,
            as_of=datetime.now(timezone.utc),
            current_price=float(current_price),
            market_cap=float(info["marketCap"]) if info.get("marketCap") else None,
            enterprise_value=float(info["enterpriseValue"]) if info.get("enterpriseValue") else None,
            high_52w=float(high_52w) if high_52w else None,
            low_52w=float(low_52w) if low_52w else None,
            price_return_1m=self._compute_period_return(closes, 21),    # ~1 month (21 trading days)
            price_return_3m=self._compute_period_return(closes, 63),    # ~3 months (63 trading days)
            price_return_6m=self._compute_period_return(closes, 126),   # ~6 months (126 trading days)
            price_return_1y=self._compute_period_return(closes, 252),   # ~1 year (252 trading days)
            price_return_3y=self._compute_period_return(closes, 756),   # ~3 years (756 trading days)
            price_return_5y=self._compute_period_return(closes, min(len(closes) - 1, 1255)) if len(closes) > 1000 else None,
        )

    # ==========================================================================
    # Historical Price Series & Benchmark Relative Strength
    # Provides raw 3–5 year daily close series for charting and compares the
    # stock's multi-year performance against the S&P 500 (`^GSPC`) benchmark.
    # ==========================================================================
    def get_historical_prices(self, ticker: str, period: str = "5y") -> List[Dict[str, Any]]:
        """Return 3–5 year daily OHLCV records (`date`, `close`, `volume`) for charting & trend analysis."""
        symbol = self._to_yf_symbol(ticker)
        hist = yf.Ticker(symbol).history(period=period, interval="1d", auto_adjust=True)
        if hist is None or hist.empty:
            return []

        records: List[Dict[str, Any]] = []
        for idx, row in hist.iterrows():
            records.append(
                {
                    "date": idx.strftime("%Y-%m-%d"),
                    "close": round(float(row["Close"]), 2),
                    "volume": int(row["Volume"]) if "Volume" in row and row["Volume"] == row["Volume"] else 0,
                }
            )
        return records

    def compare_to_benchmark(self, ticker: str) -> Dict[str, Optional[float]]:
        """Compare ticker 6m, 1y, and 3y returns against the S&P 500 (`^GSPC`) to measure alpha."""
        stock_snap = self.get_price_snapshot(ticker)
        bench_snap = self.get_price_snapshot(self.BENCHMARK_SYMBOL)

        def _alpha(stock_ret: Optional[float], bench_ret: Optional[float]) -> Optional[float]:
            if stock_ret is None or bench_ret is None:
                return None
            return round(stock_ret - bench_ret, 4)

        return {
            "alpha_6m": _alpha(stock_snap.price_return_6m, bench_snap.price_return_6m),
            "alpha_1y": _alpha(stock_snap.price_return_1y, bench_snap.price_return_1y),
            "alpha_3y": _alpha(stock_snap.price_return_3y, bench_snap.price_return_3y),
        }

    # ==========================================================================
    # Finnhub Quote Fallback (Optional Free API Key)
    # Activated only if `FINNHUB_API_KEY` is configured and Yahoo Finance fails.
    # ==========================================================================
    def _fetch_finnhub_price(self, symbol: str) -> Optional[float]:
        """Fetch latest real-time quote from Finnhub REST API (`c` = current price)."""
        if not self.finnhub_api_key:
            return None
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(
                    f"{self.FINNHUB_BASE_URL}/quote",
                    params={"symbol": symbol, "token": self.finnhub_api_key},
                )
                resp.raise_for_status()
                price = resp.json().get("c")
                return float(price) if price and float(price) > 0 else None
        except Exception:
            return None
