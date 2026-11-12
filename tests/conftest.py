"""Shared Pytest fixtures (mock SEC `companyfacts` JSON, sample news feeds, and company identity)."""

from datetime import datetime, timezone
import pytest

from dager.schemas.filings import CompanyIdentity, MarketPriceSnapshot
from dager.schemas.sentiment import SentimentDocument, SentimentSourceType
from dager.storage import get_engine, init_db, session_scope


# ==============================================================================
# In-Memory SQLite Database Fixture (Phase 4 Persistence Tests)
# Creates an isolated in-memory relational database for each test function.
# ==============================================================================
@pytest.fixture
def db_session():
    """Yield a clean SQLAlchemy Session backed by an in-memory SQLite database."""
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    with session_scope(engine) as session:
        yield session


# ==============================================================================
# Mock SEC EDGAR `companyfacts` Fixture
# Simulates two fiscal years (2024 and 2025) of 10-K XBRL `us-gaap` facts for
# Apple Inc. so XBRL parsing and financial ratios can be tested deterministically.
# ==============================================================================
@pytest.fixture
def mock_company_identity() -> CompanyIdentity:
    return CompanyIdentity(
        ticker="AAPL",
        cik="0000320193",
        company_name="Apple Inc.",
        is_sp500=True,
    )


@pytest.fixture
def mock_market_snapshot() -> MarketPriceSnapshot:
    return MarketPriceSnapshot(
        ticker="AAPL",
        current_price=200.0,
        market_cap=3_000_000_000_000.0,
        enterprise_value=3_050_000_000_000.0,
        high_52w=230.0,
        low_52w=165.0,
        price_return_6m=0.12,
        price_return_1y=0.20,
        price_return_3y=0.45,
    )


@pytest.fixture
def mock_sec_company_facts() -> dict:
    """Provide realistic multi-year 10-K XBRL `us-gaap` facts for balance sheet, income, and cash flow."""
    def _instant(val_2025: float, val_2024: float, unit: str = "USD") -> dict:
        return {
            "units": {
                unit: [
                    {"end": "2025-09-27", "val": val_2025, "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2025-11-01"},
                    {"end": "2024-09-28", "val": val_2024, "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2024-11-01"},
                ]
            }
        }

    def _duration(val_2025: float, val_2024: float, unit: str = "USD") -> dict:
        return {
            "units": {
                unit: [
                    {
                        "start": "2024-09-29",
                        "end": "2025-09-27",
                        "val": val_2025,
                        "fy": 2025,
                        "fp": "FY",
                        "form": "10-K",
                        "filed": "2025-11-01",
                    },
                    {
                        "start": "2023-10-01",
                        "end": "2024-09-28",
                        "val": val_2024,
                        "fy": 2024,
                        "fp": "FY",
                        "form": "10-K",
                        "filed": "2024-11-01",
                    },
                ]
            }
        }

    return {
        "cik": 320193,
        "entityName": "Apple Inc.",
        "facts": {
            "us-gaap": {
                # Balance Sheet tags
                "CashAndCashEquivalentsAtCarryingValue": _instant(65_000_000_000, 60_000_000_000),
                "AccountsReceivableNetCurrent": _instant(30_000_000_000, 28_000_000_000),
                "InventoryNet": _instant(7_000_000_000, 6_500_000_000),
                "AssetsCurrent": _instant(145_000_000_000, 135_000_000_000),
                "Assets": _instant(360_000_000_000, 340_000_000_000),
                "AccountsPayableCurrent": _instant(60_000_000_000, 58_000_000_000),
                "DebtCurrent": _instant(15_000_000_000, 16_000_000_000),
                "LiabilitiesCurrent": _instant(110_000_000_000, 115_000_000_000),
                "LongTermDebtNoncurrent": _instant(85_000_000_000, 90_000_000_000),
                "Liabilities": _instant(270_000_000_000, 265_000_000_000),
                "StockholdersEquity": _instant(90_000_000_000, 75_000_000_000),
                "CommonStockSharesOutstanding": _instant(15_000_000_000, 15_400_000_000, unit="shares"),
                # Income Statement tags
                "Revenues": _duration(410_000_000_000, 380_000_000_000),
                "GrossProfit": _duration(185_000_000_000, 170_000_000_000),
                "OperatingIncomeLoss": _duration(125_000_000_000, 114_000_000_000),
                "IncomeTaxExpenseBenefit": _duration(20_000_000_000, 18_000_000_000),
                "NetIncomeLoss": _duration(105_000_000_000, 96_000_000_000),
                "DepreciationDepletionAndAmortization": _duration(11_000_000_000, 11_500_000_000),
                "EarningsPerShareDiluted": _duration(7.00, 6.23, unit="USD/shares"),
                # Cash Flow tags
                "NetCashProvidedByUsedInOperatingActivities": _duration(118_000_000_000, 110_000_000_000),
                "PaymentsToAcquirePropertyPlantAndEquipment": _duration(10_000_000_000, 10_500_000_000),
                "PaymentsOfDividends": _duration(15_000_000_000, 14_800_000_000),
                "PaymentsForRepurchaseOfCommonStock": _duration(85_000_000_000, 78_000_000_000),
            }
        },
    }


# ==============================================================================
# Sample Sentiment Documents Fixture
# Provides bullish and bearish financial headlines for testing NLP sentiment
# scoring and exponential recency decay.
# ==============================================================================
@pytest.fixture
def sample_sentiment_docs() -> list[SentimentDocument]:
    now = datetime.now(timezone.utc)
    return [
        SentimentDocument(
            ticker="AAPL",
            source_type=SentimentSourceType.NEWS_ARTICLE,
            title="Apple beats quarterly earnings expectations with record services growth",
            content="Strong profit expansion and resilient demand drove an upgrade from analysts.",
            published_at=now,
        ),
        SentimentDocument(
            ticker="AAPL",
            source_type=SentimentSourceType.RSS_FEED,
            title="Apple announces dividend increase and strong free cash flow upside",
            content="Management highlighted accelerating margins and bullish long-term outlook.",
            published_at=now,
        ),
    ]

