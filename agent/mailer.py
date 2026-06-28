"""SMTP delivery of branded HTML replies with optional ZIP attachment.

The mailer is intentionally thin: it builds a multipart HTML message, attaches
the archive when present, and sends it over an authenticated SMTP connection.
The blocking smtplib call is offloaded to a worker thread so it can be awaited
from the asyncio worker pool without stalling the event loop.
"""

import asyncio
import mimetypes
import smtplib
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

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
    """Send a prepared message over an authenticated SMTP connection."""
    if not config.SMTP_USERNAME or not config.SMTP_PASSWORD:
        raise MailerError("SMTP credentials are not configured")

    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as server:
        server.ehlo()
        if config.SMTP_USE_TLS:
            server.starttls()
            server.ehlo()
        server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
        server.send_message(message)


async def send(to_email: str, subject: str, html_body: str, attachment: Path | None = None) -> None:
    """Send a branded HTML email, offloading the blocking SMTP call to a thread."""
    message = _build_message(to_email, subject, html_body, attachment)
    await asyncio.to_thread(_send_sync, message)
    logger.info(
        "email sent",
        extra={"step": "mailer.send", "to": to_email, "subject": subject},
    )
