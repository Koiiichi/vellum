"""Tests for the Gmail API mailer with the network mocked out.

A fake Gmail service records the raw MIME payload that would have been sent so
message construction and attachment handling can be verified without contacting
Google.
"""

import base64
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default

import pytest

from agent import mailer


class _FakeExecute:
    def execute(self):
        return {"id": "sent-message-id"}


class _FakeSend:
    def __init__(self, service: "_FakeGmailService"):
        self.service = service

    def send(self, userId, body):
        self.service.user_id = userId
        self.service.body = body
        return _FakeExecute()


class _FakeMessages:
    def __init__(self, service: "_FakeGmailService"):
        self.service = service

    def messages(self):
        return _FakeSend(self.service)


class _FakeGmailService:
    def __init__(self):
        self.user_id = None
        self.body = None

    def users(self):
        return _FakeMessages(self)


def _sent_message(service: _FakeGmailService) -> EmailMessage:
    raw = service.body["raw"]
    decoded = base64.urlsafe_b64decode(raw)
    return message_from_bytes(decoded, policy=default)


@pytest.fixture
def fake_gmail(monkeypatch):
    service = _FakeGmailService()

    def get_service():
        return service

    monkeypatch.setattr(mailer, "get_gmail_service", get_service)
    monkeypatch.setattr(mailer.config, "EMAIL_FROM", "vellum@example.com")
    monkeypatch.setattr(mailer.config, "EMAIL_FROM_NAME", "Vellum")
    return service


async def test_send_plain_html(fake_gmail):
    await mailer.send("dest@example.com", "[Vellum] M12205", "<p>hello</p>")

    assert fake_gmail.user_id == "me"
    message = _sent_message(fake_gmail)
    assert message["To"] == "dest@example.com"
    assert message["Subject"] == "[Vellum] M12205"
    assert "Vellum" in message["From"]
    assert message["Auto-Submitted"] == "auto-generated"
    assert message["X-Auto-Response-Suppress"] == "All"
    assert message["Precedence"] == "bulk"


async def test_send_with_attachment(fake_gmail, tmp_path):
    zip_path = tmp_path / "vellum_M12205_Exhibits_2026-06-26.zip"
    zip_path.write_bytes(b"PK\x03\x04 fake zip")

    await mailer.send("dest@example.com", "subject", "<p>body</p>", zip_path)
    message = _sent_message(fake_gmail)

    attachments = [
        part for part in message.iter_attachments()
        if part.get_filename() == zip_path.name
    ]
    assert len(attachments) == 1
