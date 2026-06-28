"""Tests for the SMTP mailer with the network mocked out.

A fake SMTP class records the message that would have been sent so message
construction, TLS negotiation, authentication, and attachment handling can be
verified without contacting a real server.
"""

import pytest

from agent import mailer


class _FakeSMTP:
    instances: list["_FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.tls = False
        self.login_args = None
        self.sent_message = None
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def ehlo(self):
        pass

    def starttls(self):
        self.tls = True

    def login(self, username, password):
        self.login_args = (username, password)

    def send_message(self, message):
        self.sent_message = message


@pytest.fixture(autouse=True)
def patch_smtp(monkeypatch):
    _FakeSMTP.instances.clear()
    monkeypatch.setattr(mailer.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(mailer.config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(mailer.config, "SMTP_PORT", 587)
    monkeypatch.setattr(mailer.config, "SMTP_USERNAME", "user@example.com")
    monkeypatch.setattr(mailer.config, "SMTP_PASSWORD", "secret")
    monkeypatch.setattr(mailer.config, "SMTP_USE_TLS", True)
    monkeypatch.setattr(mailer.config, "EMAIL_FROM", "vellum@example.com")
    monkeypatch.setattr(mailer.config, "EMAIL_FROM_NAME", "Vellum")


async def test_send_plain_html():
    await mailer.send("dest@example.com", "[Vellum] M12205", "<p>hello</p>")
    assert len(_FakeSMTP.instances) == 1
    smtp = _FakeSMTP.instances[0]
    assert smtp.tls is True
    assert smtp.login_args == ("user@example.com", "secret")
    message = smtp.sent_message
    assert message["To"] == "dest@example.com"
    assert message["Subject"] == "[Vellum] M12205"
    assert "Vellum" in message["From"]


async def test_send_with_attachment(tmp_path):
    zip_path = tmp_path / "vellum_M12205_Exhibits_2026-06-26.zip"
    zip_path.write_bytes(b"PK\x03\x04 fake zip")

    await mailer.send("dest@example.com", "subject", "<p>body</p>", zip_path)
    message = _FakeSMTP.instances[0].sent_message

    attachments = [
        part for part in message.iter_attachments()
        if part.get_filename() == zip_path.name
    ]
    assert len(attachments) == 1


async def test_send_raises_without_credentials(monkeypatch):
    monkeypatch.setattr(mailer.config, "SMTP_USERNAME", None)
    monkeypatch.setattr(mailer.config, "SMTP_PASSWORD", None)
    with pytest.raises(mailer.MailerError):
        await mailer.send("dest@example.com", "subject", "<p>body</p>")
