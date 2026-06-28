"""Gmail API delivery of branded HTML replies with optional ZIP attachment.

The mailer is intentionally thin: it builds a multipart HTML message, attaches
the archive when present, and sends it through the authenticated Gmail API. The
blocking Google API call is offloaded to a worker thread so it can be awaited
from the asyncio worker pool without stalling the event loop.
"""

import asyncio
import base64
import mimetypes
import re
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

from googleapiclient.errors import HttpError

from agent.gmail_watch import get_gmail_service
from core import config
from core.logger import get_logger

logger = get_logger(__name__)
_RETRY_AFTER_PATTERN = re.compile(r"Retry after ([0-9T:.-]+Z)")


class MailerError(Exception):
    """Raised when an email cannot be constructed or delivered."""


class MailerRateLimitExceeded(MailerError):
    """Raised when Gmail keeps rejecting delivery after retry attempts."""


def _build_message(to_email: str, subject: str, html_body: str, attachment: Path | None) -> EmailMessage:
    """Construct an HTML EmailMessage with an optional file attachment."""
    message = EmailMessage()
    message["From"] = formataddr((config.EMAIL_FROM_NAME, config.EMAIL_FROM or ""))
    message["To"] = to_email
    message["Subject"] = subject
    message["Auto-Submitted"] = "auto-generated"
    message["X-Auto-Response-Suppress"] = "All"
    message["Precedence"] = "bulk"
    message.set_content(
        "Your email client does not support HTML. Please view this message in "
        "an HTML-capable client to see your Vellum results."
    )
    message.add_alternative(html_body, subtype="html")

    if attachment is not None:
        attachment = Path(attachment)
        mime_type, _ = mimetypes.guess_type(attachment.name)
        maintype, subtype = (mime_type or "application/zip").split("/", 1)
        message.add_attachment(
            attachment.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=attachment.name,
        )
    return message


def _send_sync(message: EmailMessage) -> None:
    """Send a prepared message through the authenticated Gmail API."""
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    service = get_gmail_service()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()


def _retry_after_delay(error: HttpError, now: datetime | None = None) -> float | None:
    """Return Gmail's requested retry delay in seconds when present."""
    retry_after = getattr(error.resp, "get", lambda _name: None)("retry-after")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            pass

    content = error.content.decode("utf-8", "replace") if isinstance(error.content, bytes) else str(error.content)
    match = _RETRY_AFTER_PATTERN.search(content)
    if not match:
        return None

    retry_at = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
    now = now or datetime.now(UTC)
    return max(0.0, (retry_at - now).total_seconds())


def _is_rate_limit_error(error: HttpError) -> bool:
    """Return True when Gmail rejected the send because of rate limiting."""
    status = getattr(error.resp, "status", None)
    if status == 429:
        return True
    content = error.content.decode("utf-8", "replace") if isinstance(error.content, bytes) else str(error.content)
    return "rateLimitExceeded" in content or "User-rate limit exceeded" in content


async def send(to_email: str, subject: str, html_body: str, attachment: Path | None = None) -> None:
    """Send a branded HTML email, offloading the blocking SMTP call to a thread."""
    message = _build_message(to_email, subject, html_body, attachment)
    attempts = max(1, config.MAILER_SEND_RETRY_ATTEMPTS)
    for attempt in range(1, attempts + 1):
        try:
            await asyncio.to_thread(_send_sync, message)
            break
        except HttpError as exc:
            if not _is_rate_limit_error(exc) or attempt >= attempts:
                if _is_rate_limit_error(exc):
                    raise MailerRateLimitExceeded(str(exc)) from exc
                raise

            provider_delay = _retry_after_delay(exc)
            fallback_delay = config.MAILER_RETRY_BASE_DELAY_S * (2 ** (attempt - 1))
            delay = provider_delay if provider_delay is not None else fallback_delay
            delay = min(delay, config.MAILER_MAX_RETRY_DELAY_S)
            logger.warning(
                "gmail send rate limited, retrying",
                extra={
                    "step": "mailer.rate_limit",
                    "to": to_email,
                    "subject": subject,
                    "attempt": attempt,
                    "max_attempts": attempts,
                    "retry_delay_s": round(delay, 3),
                },
            )
            await asyncio.sleep(delay)

    logger.info(
        "email sent",
        extra={"step": "mailer.send", "to": to_email, "subject": subject},
    )
