"""App settings, logging, and environment parsing."""

from functools import lru_cache
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Load `.env` into `os.environ` immediately so third-party SDKs (like Prefect Cloud)
# automatically see `PREFECT_API_KEY` and `PREFECT_API_URL`.
load_dotenv()
if os.environ.get("PREFECT_API_KEY"):
    os.environ.pop("PREFECT_API_AUTH_STRING", None)
if "PREFECT_API_URL" in os.environ and "app.prefect.cloud/account/" in os.environ["PREFECT_API_URL"]:
    os.environ["PREFECT_API_URL"] = (
        os.environ["PREFECT_API_URL"]
        .replace("https://app.prefect.cloud/account/", "https://api.prefect.cloud/api/accounts/")
        .replace("/workspace/", "/workspaces/")
    )


# ==============================================================================
# Application Settings Model
# Centralizes all environment variables with type validation and sensible defaults
# using Pydantic Settings. Automatically loads values from a local `.env` file.
# ==============================================================================
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- SEC EDGAR Configuration ---
    # SEC requires a descriptive User-Agent header in the format "AppName ContactEmail"
    sec_user_agent: str = Field(
        default="DAGER-Research contact@example.com",
        description="User-Agent string required by SEC EDGAR fair access policy.",
    )
    sec_min_interval_seconds: float = Field(
        default=0.15,
        ge=0.1,
        description="Minimum delay between SEC requests (0.15s = ~6.6 req/sec, well under 10 req/sec limit).",
    )

    # --- Database Configuration ---
    # Supports SQLite out of the box and PostgreSQL in production deployments
    database_url: str = Field(
        default="sqlite:///./dager.db",
        description="SQLAlchemy connection URI for relational persistence.",
    )

    # --- External API Keys ---
    # Optional keys for supplemental news and market data providers
    news_api_key: Optional[str] = Field(
        default=None,
        description="Optional API key for NewsAPI or supplemental financial news providers.",
    )
    finnhub_api_key: Optional[str] = Field(
        default=None,
        description="Optional API key for Finnhub market/news endpoints.",
    )

    # --- NLP & Sentiment Configuration ---
    # Configures the HuggingFace FinBERT model and exponential recency decay
    finbert_model_name: str = Field(
        default="ProsusAI/finbert",
        description="HuggingFace model identifier for financial sentiment scoring.",
    )
    sentiment_decay_half_life_days: float = Field(
        default=14.0,
        gt=0.0,
        description="Half-life in days for weighting recent news more heavily than older articles.",
    )

    # --- Cache & Pillar Weight Settings ---
    # Controls cache freshness for S&P 500 batch runs vs. on-demand user queries
    sp500_cache_ttl_hours: int = Field(
        default=24,
        ge=1,
        description="Time-to-live (hours) before pre-computed S&P 500 evaluations are considered stale.",
    )
    ondemand_cache_ttl_hours: int = Field(
        default=12,
        ge=1,
        description="Time-to-live (hours) before an on-demand ticker evaluation is re-computed.",
    )

    # --- Logging Configuration ---
    log_level: str = Field(
        default="INFO",
        description="Application-wide logging verbosity (DEBUG, INFO, WARNING, ERROR).",
    )


# ==============================================================================
# Singleton Settings Accessor & Logging Helper
# Caches the parsed Settings object so `.env` is read only once per process,
# and provides a standard logger factory for all DAGER modules.
# ==============================================================================
@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached singleton instance of application Settings."""
    return Settings()


def configure_logging(level: Optional[str] = None, log_file: str = "logs/dager.log") -> logging.Logger:
    """
    Configure root DAGER logger with both console output and a persistent
    rotating log file (`logs/dager.log`) so all pipeline runs are collected.
    """
    effective_level = getattr(logging, (level or get_settings().log_level).upper(), logging.INFO)
    log_format = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    formatter = logging.Formatter(log_format, datefmt="%Y-%m-%d %H:%M:%S")

    # Ensure `logs/` directory exists for persistent log collection
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger("dager")
    root_logger.setLevel(effective_level)

    if not root_logger.handlers:
        # 1. Console Stream Handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(effective_level)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

        # 2. Rotating File Handler (5 MB per file, keeps last 3 log files)
        file_handler = RotatingFileHandler(
            log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setLevel(effective_level)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    return root_logger


# Convenience module-level settings instance
settings = get_settings()

