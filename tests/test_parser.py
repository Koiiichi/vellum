"""Unit tests for agent.parser with the LLM call mocked out.

These tests never reach the network: a fake AsyncOpenAI client returns a
canned JSON payload so the normalisation, subject gate, and rate limiter logic
can be verified deterministically.
"""

import json
import types

import pytest

from agent import parser
from core.models import VALID_DOC_TYPES


class _FakeCompletions:
    def __init__(self, content: str) -> None:
        self._content = content

    async def create(self, *args, **kwargs):
        message = types.SimpleNamespace(content=self._content)
        choice = types.SimpleNamespace(message=message)
        return types.SimpleNamespace(choices=[choice])


class _FakeChat:
    def __init__(self, content: str) -> None:
        self.completions = _FakeCompletions(content)


class _FakeClient:
    def __init__(self, content: str) -> None:
        self.chat = _FakeChat(content)


@pytest.fixture(autouse=True)
def reset_rate_limiter(monkeypatch):
    """Give each test a fresh, generous rate limiter."""
    monkeypatch.setattr(
        parser,
        "_rate_limiter",
        parser.SlidingWindowRateLimiter(max_calls_per_minute=1000),
    )


def _mock_model(monkeypatch, payload: dict) -> None:
    """Patch the parser client to return the given payload as JSON."""
    content = json.dumps(payload)
    monkeypatch.setattr(parser, "_get_client", lambda: _FakeClient(content))


@pytest.mark.asyncio
async def test_single_type(monkeypatch):
    _mock_model(monkeypatch, {"matter_number": "M12205", "document_types": ["Other Documents"]})
    result = await parser.parse("a@b.com", "[vellum] request", "Other Documents from M12205?", "rid")
    assert result is not None
    assert result.matter_number == "M12205"
    assert result.document_types == ["Other Documents"]


@pytest.mark.asyncio
async def test_multi_type(monkeypatch):
    _mock_model(monkeypatch, {"matter_number": "M12383", "document_types": ["Exhibits", "Transcripts"]})
    result = await parser.parse("a@b.com", "[vellum] hi", "Exhibits and Transcripts for M12383", "rid")
    assert result.matter_number == "M12383"
    assert result.document_types == ["Exhibits", "Transcripts"]


@pytest.mark.asyncio
async def test_all_documents(monkeypatch):
    all_types = list(VALID_DOC_TYPES)
    _mock_model(monkeypatch, {"matter_number": "M12205", "document_types": all_types})
    result = await parser.parse("a@b.com", "[vellum] all", "everything from M12205", "rid")
    assert set(result.document_types) == VALID_DOC_TYPES


@pytest.mark.asyncio
async def test_ambiguous_no_matter_no_types(monkeypatch):
    _mock_model(monkeypatch, {"matter_number": None, "document_types": []})
    result = await parser.parse("a@b.com", "[vellum] help", "Can you help with regulatory filings?", "rid")
    assert result.matter_number is None
    assert result.document_types == []


@pytest.mark.asyncio
async def test_missing_matter(monkeypatch):
    _mock_model(monkeypatch, {"matter_number": None, "document_types": ["Exhibits"]})
    result = await parser.parse("a@b.com", "[vellum] x", "send me exhibits", "rid")
    assert result.matter_number is None
    assert result.document_types == ["Exhibits"]


@pytest.mark.asyncio
async def test_missing_type(monkeypatch):
    _mock_model(monkeypatch, {"matter_number": "M12205", "document_types": []})
    result = await parser.parse("a@b.com", "[vellum] x", "I need M12205", "rid")
    assert result.matter_number == "M12205"
    assert result.document_types == []


@pytest.mark.asyncio
async def test_invalid_matter_format_normalised_to_none(monkeypatch):
    _mock_model(monkeypatch, {"matter_number": "12205", "document_types": ["Exhibits"]})
    result = await parser.parse("a@b.com", "[vellum] x", "12205 exhibits", "rid")
    assert result.matter_number is None


@pytest.mark.asyncio
async def test_lowercase_matter_normalised(monkeypatch):
    _mock_model(monkeypatch, {"matter_number": "m12205", "document_types": ["Exhibits"]})
    result = await parser.parse("a@b.com", "[vellum] x", "m12205 exhibits", "rid")
    assert result.matter_number == "M12205"


@pytest.mark.asyncio
async def test_invalid_doc_types_filtered_and_deduped(monkeypatch):
    _mock_model(
        monkeypatch,
        {"matter_number": "M12205", "document_types": ["Exhibits", "Exhibits", "Bananas"]},
    )
    result = await parser.parse("a@b.com", "[vellum] x", "exhibits", "rid")
    assert result.document_types == ["Exhibits"]


@pytest.mark.asyncio
async def test_malformed_json_response(monkeypatch):
    monkeypatch.setattr(parser, "_get_client", lambda: _FakeClient("not json at all"))
    result = await parser.parse("a@b.com", "[vellum] x", "exhibits", "rid")
    assert result.matter_number is None
    assert result.document_types == []


@pytest.mark.asyncio
async def test_code_fenced_json_response(monkeypatch):
    fenced = "```json\n{\"matter_number\": \"M12205\", \"document_types\": [\"Exhibits\"]}\n```"
    monkeypatch.setattr(parser, "_get_client", lambda: _FakeClient(fenced))
    result = await parser.parse("a@b.com", "[vellum] x", "exhibits", "rid")
    assert result.matter_number == "M12205"
    assert result.document_types == ["Exhibits"]


@pytest.mark.asyncio
async def test_subject_gate_blocks_missing_tag(monkeypatch):
    _mock_model(monkeypatch, {"matter_number": "M12205", "document_types": ["Exhibits"]})
    result = await parser.parse("a@b.com", "regular subject", "Exhibits from M12205", "rid")
    assert result is None


@pytest.mark.asyncio
async def test_subject_gate_is_case_insensitive(monkeypatch):
    _mock_model(monkeypatch, {"matter_number": "M12205", "document_types": ["Exhibits"]})
    result = await parser.parse("a@b.com", "[VELLUM] Request", "Exhibits from M12205", "rid")
    assert result is not None


@pytest.mark.asyncio
async def test_subject_gate_blocks_none_subject(monkeypatch):
    _mock_model(monkeypatch, {"matter_number": "M12205", "document_types": ["Exhibits"]})
    result = await parser.parse("a@b.com", None, "Exhibits from M12205", "rid")
    assert result is None


@pytest.mark.asyncio
async def test_rate_limiter_raises_when_exhausted():
    limiter = parser.SlidingWindowRateLimiter(max_calls_per_minute=2)
    await limiter.acquire()
    await limiter.acquire()
    with pytest.raises(parser.RateLimitExceeded):
        await limiter.acquire()
