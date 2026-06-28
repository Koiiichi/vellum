"""Tests for Gmail listener message normalization and filtering."""

from agent import listener


def test_parse_message_normalises_sender_email():
    raw = {
        "payload": {
            "headers": [
                {"name": "From", "value": "Vellum <VellumTheAgent@gmail.com>"},
                {"name": "Subject", "value": "[Vellum] M12205"},
            ],
            "mimeType": "text/plain",
            "body": {"data": "SGVsbG8="},
        }
    }

    sender, subject, body = listener._parse_message(raw)

    assert sender == "vellumtheagent@gmail.com"
    assert subject == "[Vellum] M12205"
    assert body == "Hello"


def test_self_sender_matches_configured_mailbox(monkeypatch):
    monkeypatch.setattr(listener.config, "GMAIL_ADDRESS", "vellumtheagent@gmail.com")
    monkeypatch.setattr(listener.config, "EMAIL_FROM", "Vellum <vellumtheagent@gmail.com>")

    assert listener._is_self_sender("Vellum <VellumTheAgent@gmail.com>") is True
    assert listener._is_self_sender("requester@example.com") is False
