"""Resend delivery of branded HTML replies.

The mailer is intentionally thin: it builds a multipart HTML message, attaches
the archive when present, and sends it through Resend's Python SDK. The blocking
network call is offloaded to a worker thread so it can be awaited from the
asyncio worker pool without stalling the event loop.
"""

import asyncio
import mimetypes
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

import resend

from core import config
from core.logger import get_logger

logger = get_logger(__name__)


class MailerError(Exception):
    """Raised when an email cannot be constructed or delivered."""


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


def _extract_html_body(message: EmailMessage) -> str:
    """Extract the HTML alternative from a MIME message."""
    html_part = message.get_body(preferencelist=("html",))
    if html_part is not None:
        return html_part.get_content()

    if message.get_content_type() == "text/html":
        return message.get_content()

    plain_part = message.get_body(preferencelist=("plain",))
    if plain_part is not None:
        return plain_part.get_content()

    return ""


def _send_sync(message: EmailMessage) -> None:
    """Send a prepared message through Resend's Python SDK."""
    api_key = config.RESEND_API_KEY
    from_email = config.EMAIL_FROM
    if not api_key:
        raise MailerError("RESEND_API_KEY is not configured")
    if not from_email:
        raise MailerError("EMAIL_FROM is not configured")

    payload = {
        "from": from_email,
        "to": [message["To"]],
        "subject": message["Subject"],
        "html": _extract_html_body(message),
    }

    try:
        if hasattr(resend, "Resend"):
            client = resend.Resend(api_key=api_key)
            client.emails.send(payload)
        else:
            resend.api_key = api_key
            resend.Emails.send(payload)
    except Exception as exc:
        logger.error(
            "resend delivery failed",
            extra={
                "step": "mailer.resend",
                "error_type": type(exc).__name__,
                "reason": str(exc),
                "to": message["To"],
                "subject": message["Subject"],
            },
        )
        raise MailerError(f"Resend delivery failed: {exc}") from exc


async def send(to_email: str, subject: str, html_body: str, attachment: Path | None = None) -> None:
    """Send a branded HTML email through Resend."""
    message = _build_message(to_email, subject, html_body, attachment)
    await asyncio.to_thread(_send_sync, message)
    logger.info(
        "email sent",
        extra={"step": "mailer.send", "to": to_email, "subject": subject},
    )
