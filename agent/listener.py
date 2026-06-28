"""FastAPI webhook handler for Gmail Push notifications.

Google Cloud Pub/Sub delivers a notification containing only a ``historyId``
shortly after a message arrives. This handler decodes that notification, asks
the Gmail API which messages are new since the last seen ``historyId``, fetches
each message, parses it, and enqueues a job. The HTTP 200 is returned before any
scraping begins so Pub/Sub receives a fast acknowledgement and the asyncio
worker pool processes the job out of band.
"""

import asyncio
import base64
import json
from email.utils import parseaddr

from fastapi import FastAPI, Request

from agent import parser
from agent.gmail_watch import get_gmail_service
from core import config
from core.logger import get_logger, new_request_id
from core.models import ParsedRequest

logger = get_logger(__name__)

_last_history_id: int | None = None


def set_history_id(history_id: int | str | None) -> None:
    """Seed the last seen Gmail historyId, typically from watch() at startup."""
    global _last_history_id
    if history_id is not None:
        _last_history_id = int(history_id)


def _decode_b64(data: str) -> bytes:
    """Decode a base64url string, tolerating missing padding."""
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _header(payload: dict, name: str) -> str:
    """Return a named header value from a Gmail message payload."""
    for header in payload.get("headers", []):
        if header.get("name", "").lower() == name.lower():
            return header.get("value", "")
    return ""


def _normalise_email(value: str | None) -> str:
    """Extract and lowercase an email address from a header value."""
    _, address = parseaddr(value or "")
    return address.strip().lower()


def _is_self_sender(sender: str) -> bool:
    """Return True when a message came from Vellum's own mailbox."""
    sender = _normalise_email(sender)
    own_addresses = {
        _normalise_email(config.GMAIL_ADDRESS),
        _normalise_email(config.EMAIL_FROM),
    }
    own_addresses.discard("")
    return bool(sender and sender in own_addresses)


def _extract_body(payload: dict) -> str:
    """Extract the best-effort plain-text body from a Gmail message payload."""
    mime_type = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data")

    if mime_type == "text/plain" and body_data:
        return _decode_b64(body_data).decode("utf-8", "replace")

    for part in payload.get("parts", []):
        text = _extract_body(part)
        if text:
            return text

    if body_data:
        return _decode_b64(body_data).decode("utf-8", "replace")
    return ""


def _list_new_message_ids(service, start_history_id: int) -> list[str]:
    """List message ids added since the given historyId. Blocking."""
    message_ids: list[str] = []
    response = (
        service.users()
        .history()
        .list(userId="me", startHistoryId=start_history_id, historyTypes=["messageAdded"])
        .execute()
    )
    for record in response.get("history", []):
        for added in record.get("messagesAdded", []):
            message = added.get("message", {})
            if message.get("id"):
                message_ids.append(message["id"])
    return message_ids


def _get_message(service, message_id: str) -> dict:
    """Fetch a full Gmail message by id. Blocking."""
    return service.users().messages().get(userId="me", id=message_id, format="full").execute()


def _parse_message(raw: dict) -> tuple[str, str, str]:
    """Return (sender_email, subject, body) from a raw Gmail message."""
    payload = raw.get("payload", {})
    sender = _normalise_email(_header(payload, "From"))
    subject = _header(payload, "Subject")
    body = _extract_body(payload)
    return sender, subject, body


async def _handle_notification(notification: dict, queue: "asyncio.Queue[ParsedRequest]") -> None:
    """Decode a Pub/Sub notification and enqueue any new parseable requests."""
    global _last_history_id

    message = notification.get("message", {})
    data = message.get("data")
    if not data:
        logger.warning("notification missing data field", extra={"step": "listener.decode"})
        return

    decoded = json.loads(_decode_b64(data).decode("utf-8", "replace"))
    history_id = decoded.get("historyId")
    if history_id is None:
        return

    if _last_history_id is None:
        _last_history_id = int(history_id)
        logger.info("seeded historyId from first notification", extra={"step": "listener.seed"})
        return

    service = get_gmail_service()
    start_history_id = _last_history_id
    _last_history_id = int(history_id)

    message_ids = await asyncio.to_thread(_list_new_message_ids, service, start_history_id)
    for message_id in message_ids:
        raw = await asyncio.to_thread(_get_message, service, message_id)
        sender, subject, body = _parse_message(raw)
        if _is_self_sender(sender):
            logger.info(
                "email ignored: sender is Vellum mailbox",
                extra={"step": "listener.self_sender", "message_id": message_id},
            )
            continue
        request_id = new_request_id()
        try:
            parsed = await parser.parse(sender, subject, body, request_id)
        except parser.RateLimitExceeded:
            logger.warning(
                "parser rate limit exceeded, dropping message",
                extra={"step": "listener.rate_limit", "message_id": message_id},
            )
            continue
        if parsed is not None:
            await queue.put(parsed)
            logger.info(
                "request enqueued",
                extra={
                    "step": "listener.enqueue",
                    "matter_number": parsed.matter_number,
                    "document_types": parsed.document_types,
                },
            )


def create_app(queue: "asyncio.Queue[ParsedRequest]") -> FastAPI:
    """Create the FastAPI application bound to the shared job queue."""
    app = FastAPI(title="Vellum", docs_url=None, redoc_url=None)
    app.state.queue = queue

    @app.get("/health")
    async def health() -> dict:
        """Liveness probe."""
        return {"status": "ok"}

    @app.post("/gmail/webhook")
    async def gmail_webhook(request: Request) -> dict:
        """Acknowledge a Pub/Sub push and enqueue any new requests."""
        try:
            notification = await request.json()
            await _handle_notification(notification, app.state.queue)
        except Exception:
            logger.exception("failed to handle webhook", extra={"step": "listener.webhook"})
        return {"status": "ok"}

    return app
