"""Tests for the Resend SDK mailer with the network mocked out."""

from email.message import EmailMessage

import pytest

from agent import mailer


class _FakeEmails:
    sent_payloads = []

    @classmethod
    def send(cls, payload):
        cls.sent_payloads.append(payload)
        return {"id": "email-id"}


@pytest.fixture
def fake_resend(monkeypatch):
    _FakeEmails.sent_payloads = []
    monkeypatch.delattr(mailer.resend, "Resend", raising=False)
    monkeypatch.setattr(mailer.resend, "Emails", _FakeEmails)
    monkeypatch.setattr(mailer.config, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(mailer.config, "EMAIL_FROM", "Vellum <vellum@example.com>")
    monkeypatch.setattr(mailer.config, "EMAIL_FROM_NAME", "Vellum")
    return _FakeEmails.sent_payloads


async def test_send_plain_html(fake_resend):
    await mailer.send("dest@example.com", "[Vellum] M12205", "<p>hello</p>")

    assert fake_resend == [
        {
            "from": "Vellum <vellum@example.com>",
            "to": ["dest@example.com"],
            "subject": "[Vellum] M12205",
            "html": "<p>hello</p>\n",
            "text": "Your email client does not support HTML. Please view this message in an HTML-capable client to see your Vellum results.\n",
        }
    ]


async def test_send_with_attachment_posts_resend_payload(fake_resend, tmp_path):
    zip_path = tmp_path / "vellum_M12205_Exhibits_2026-06-26.zip"
    zip_path.write_bytes(b"PK\x03\x04 fake zip")

    await mailer.send("dest@example.com", "subject", "<p>body</p>", zip_path)

    assert fake_resend[0]["to"] == ["dest@example.com"]
    assert fake_resend[0]["subject"] == "subject"
    assert fake_resend[0]["html"] == "<p>body</p>\n"
    assert fake_resend[0]["text"].startswith("Your email client does not support HTML.")


def test_extract_html_body_prefers_html_alternative():
    message = EmailMessage()
    message.set_content("plain body")
    message.add_alternative("<p>html body</p>", subtype="html")

    assert mailer._extract_html_body(message) == "<p>html body</p>\n"


def test_extract_text_body_prefers_plain_alternative():
    message = EmailMessage()
    message.set_content("plain body")
    message.add_alternative("<p>html body</p>", subtype="html")

    assert mailer._extract_text_body(message) == "plain body\n"


async def test_send_requires_resend_api_key(monkeypatch):
    monkeypatch.setattr(mailer.config, "RESEND_API_KEY", None)
    monkeypatch.setattr(mailer.config, "EMAIL_FROM", "Vellum <vellum@example.com>")

    with pytest.raises(mailer.MailerError, match="RESEND_API_KEY"):
        await mailer.send("dest@example.com", "subject", "<p>body</p>")


async def test_send_wraps_resend_sdk_error(monkeypatch):
    class FailingEmails:
        @classmethod
        def send(cls, payload):
            raise RuntimeError("invalid from")

    monkeypatch.delattr(mailer.resend, "Resend", raising=False)
    monkeypatch.setattr(mailer.resend, "Emails", FailingEmails)
    monkeypatch.setattr(mailer.config, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(mailer.config, "EMAIL_FROM", "Vellum <vellum@example.com>")

    with pytest.raises(mailer.MailerError, match="invalid from"):
        await mailer.send("dest@example.com", "subject", "<p>body</p>")


async def test_send_supports_resend_client_shape(monkeypatch):
    sent_payloads = []

    class FakeClient:
        def __init__(self, api_key):
            self.api_key = api_key
            self.emails = self

        def send(self, payload):
            sent_payloads.append((self.api_key, payload))
            return {"id": "email-id"}

    monkeypatch.setattr(mailer.resend, "Resend", FakeClient, raising=False)
    monkeypatch.setattr(mailer.config, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(mailer.config, "EMAIL_FROM", "Vellum <vellum@example.com>")

    await mailer.send("dest@example.com", "subject", "<p>body</p>")

    assert sent_payloads == [
        (
            "re_test",
            {
                "from": "Vellum <vellum@example.com>",
                "to": ["dest@example.com"],
                "subject": "subject",
                "html": "<p>body</p>\n",
                "text": "Your email client does not support HTML. Please view this message in an HTML-capable client to see your Vellum results.\n",
            },
        )
    ]
