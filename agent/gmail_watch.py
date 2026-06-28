import os
import asyncio
import logging
from pathlib import Path
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

_repo_root = Path(__file__).resolve().parents[1]
load_dotenv(_repo_root / ".local.env")
load_dotenv()

PUBSUB_TOPIC = os.getenv("PUBSUB_TOPIC")
GMAIL_TOKEN_PATH = os.getenv("GMAIL_TOKEN_PATH", "token.json")
GMAIL_CREDENTIALS_PATH = os.getenv("GMAIL_CREDENTIALS_PATH", "credentials.json")
WATCH_RENEWAL_INTERVAL_S = 60 * 60 * 24


def get_gmail_service():
    creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_PATH)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        try:
            with open(GMAIL_TOKEN_PATH, "w") as f:
                f.write(creds.to_json())
        except OSError as exc:
            logger.warning(
                f"could not persist refreshed token to {GMAIL_TOKEN_PATH}: {exc}. "
                "Continuing with the in-memory token."
            )
    return build("gmail", "v1", credentials=creds)


def renew_watch():
    """Call Gmail API watch() and return the result. Blocking."""
    if not PUBSUB_TOPIC:
        raise RuntimeError(
            "PUBSUB_TOPIC is not set. Add it to .local.env or the environment before starting Vellum."
        )
    service = get_gmail_service()
    result = service.users().watch(
        userId="me",
        body={
            "topicName": PUBSUB_TOPIC,
            "labelIds": ["INBOX"],
            "labelFilterBehavior": "INCLUDE",
        }
    ).execute()
    logger.info(f"watch() renewed — expiry: {result['expiration']}, historyId: {result['historyId']}")
    return result


async def watch_renewal_loop() -> None:
    """
    Runs forever inside the asyncio event loop.
    Renews watch() once at startup, then every 24 hours.
    """
    while True:
        try:
            renew_watch()
        except Exception as e:
            logger.error(f"watch() renewal failed: {e}")
        await asyncio.sleep(WATCH_RENEWAL_INTERVAL_S)