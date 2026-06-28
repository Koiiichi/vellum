"""Central configuration loaded from environment variables.

Loads .local.env first (developer overrides, gitignored), then .env as a
fallback, then any variables already present in the process environment. All
runtime configuration for Vellum is exposed as module-level constants so the
rest of the codebase imports from a single source of truth.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]

load_dotenv(REPO_ROOT / ".local.env")
load_dotenv(REPO_ROOT / ".env")


def _get_str(name: str, default: str | None = None) -> str | None:
    """Return the string value of an environment variable or a default."""
    value = os.getenv(name)
    return value if value is not None else default


def _get_int(name: str, default: int) -> int:
    """Return the integer value of an environment variable or a default."""
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


def _get_bool(name: str, default: bool) -> bool:
    """Return the boolean value of an environment variable or a default."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


GMAIL_ADDRESS = _get_str("GMAIL_ADDRESS")
GMAIL_CREDENTIALS_PATH = _get_str("GMAIL_CREDENTIALS_PATH", "credentials.json")
GMAIL_TOKEN_PATH = _get_str("GMAIL_TOKEN_PATH", "token.json")
PUBSUB_TOPIC = _get_str("PUBSUB_TOPIC")
PUBSUB_SUBSCRIPTION = _get_str("PUBSUB_SUBSCRIPTION")

OPENAI_API_KEY = _get_str("OPENAI_API_KEY")
OPENAI_MODEL = _get_str("OPENAI_MODEL", "gpt-5.4-mini")

MAX_CONCURRENT_WORKERS = _get_int("MAX_CONCURRENT_WORKERS", 2)
MAX_DOCUMENTS = _get_int("MAX_DOCUMENTS", 10)
DOWNLOAD_TIMEOUT_MS = _get_int("DOWNLOAD_TIMEOUT_MS", 30000)
DOWNLOAD_START_TIMEOUT_MS = _get_int("DOWNLOAD_START_TIMEOUT_MS", 120000)
SCRAPER_RETRY_ATTEMPTS = _get_int("SCRAPER_RETRY_ATTEMPTS", 3)
SCRAPER_RETRY_BACKOFF_S = _get_int("SCRAPER_RETRY_BACKOFF_S", 2)

PARSER_MAX_CALLS_PER_MINUTE = _get_int("PARSER_MAX_CALLS_PER_MINUTE", 20)

UARB_BASE_URL = _get_str("UARB_BASE_URL", "https://uarb.novascotia.ca/fmi/webd/UARB15")
SELECTOR_TIMEOUT_MS = _get_int("SELECTOR_TIMEOUT_MS", 15000)
SCRAPER_HEADLESS = _get_bool("SCRAPER_HEADLESS", True)

EMAIL_FROM = _get_str("EMAIL_FROM") or GMAIL_ADDRESS
EMAIL_FROM_NAME = _get_str("EMAIL_FROM_NAME", "Vellum")

GCS_BUCKET = _get_str("GCS_BUCKET")
MAX_ATTACHMENT_MB = _get_int("MAX_ATTACHMENT_MB", 24)
DELIVERY_RETENTION_DAYS = _get_int("DELIVERY_RETENTION_DAYS", 7)
EMAIL_LOGO_URL = _get_str("EMAIL_LOGO_URL")
EMAIL_LOGO_DARK_URL = _get_str("EMAIL_LOGO_DARK_URL")

HOST = _get_str("HOST", "0.0.0.0")
PORT = _get_int("PORT", 8000)
