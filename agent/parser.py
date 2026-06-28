"""LLM-based email parsing into structured requests.

A single GPT call extracts the matter number and requested document types from
free-form email text. Two guards sit in front of the model call:

* a subject gate that ignores any email whose subject does not contain the
  ``[vellum]`` tag, so the agent never burns tokens on unrelated mail; and
* an async sliding-window rate limiter that bounds the number of model calls
  per minute, protecting the OpenAI budget against bursts of (legitimate or
  spam) matter requests.

The parsed result is validated into a :class:`ParsedRequest`. A ``None`` matter
number or empty ``document_types`` list is a valid parse outcome that downstream
code turns into a branded error reply.
"""

import asyncio
import json
import re
import time

from openai import AsyncOpenAI

from core import config
from core.logger import get_logger
from core.models import VALID_DOC_TYPES, ParsedRequest

logger = get_logger(__name__)

SUBJECT_GATE_TAG = "[vellum]"

PROMPT_TEMPLATE = """Extract the matter number and document types from this email.

Matter numbers follow the pattern: letter M followed by exactly 5 digits (e.g. M12205).

Document types must come from this exact list only:
- Exhibits
- Key Documents
- Other Documents
- Transcripts
- Recordings

Return JSON only - no preamble, no markdown fences:
{{"matter_number": "M12205", "document_types": ["Exhibits", "Other Documents"]}}

Rules:
- document_types is always a list, even if only one type is requested
- If the user says "all", "everything", or "all documents", return all five types
- If no valid document types can be identified, return an empty list []
- If the matter number cannot be identified, set it to null

Email:
{email_body}
"""

_MATTER_PATTERN = re.compile(r"^M\d{5}$")


class RateLimitExceeded(Exception):
    """Raised when the parser rate limit is exhausted for the current window."""


class SlidingWindowRateLimiter:
    """Async sliding-window rate limiter bounding calls per 60 second window."""

    def __init__(self, max_calls_per_minute: int) -> None:
        self._max_calls = max_calls_per_minute
        self._window_s = 60.0
        self._timestamps: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Record a call if within budget, otherwise raise RateLimitExceeded."""
        async with self._lock:
            now = time.monotonic()
            cutoff = now - self._window_s
            self._timestamps = [t for t in self._timestamps if t > cutoff]
            if len(self._timestamps) >= self._max_calls:
                raise RateLimitExceeded(
                    f"parser rate limit of {self._max_calls}/min exceeded"
                )
            self._timestamps.append(now)


_rate_limiter = SlidingWindowRateLimiter(config.PARSER_MAX_CALLS_PER_MINUTE)
_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    """Return a lazily constructed shared AsyncOpenAI client."""
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
    return _client


def subject_is_vellum_request(subject: str | None) -> bool:
    """Return True when the email subject carries the ``[vellum]`` tag."""
    if not subject:
        return False
    return SUBJECT_GATE_TAG in subject.lower()


def _strip_code_fences(text: str) -> str:
    """Remove surrounding markdown code fences from a model response."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\n?", "", stripped)
        stripped = re.sub(r"\n?```$", "", stripped)
    return stripped.strip()


def _normalise(raw: dict, request_id: str, sender_email: str) -> ParsedRequest:
    """Validate a decoded model response into a ParsedRequest."""
    matter_number = raw.get("matter_number")
    if isinstance(matter_number, str):
        matter_number = matter_number.strip().upper()
        if not _MATTER_PATTERN.match(matter_number):
            matter_number = None
    else:
        matter_number = None

    raw_types = raw.get("document_types") or []
    document_types: list[str] = []
    if isinstance(raw_types, list):
        for item in raw_types:
            if isinstance(item, str) and item in VALID_DOC_TYPES and item not in document_types:
                document_types.append(item)

    return ParsedRequest(
        request_id=request_id,
        sender_email=sender_email,
        matter_number=matter_number,
        document_types=document_types,
    )


async def parse(
    sender_email: str,
    subject: str | None,
    body: str,
    request_id: str,
) -> ParsedRequest | None:
    """Parse an email into a ParsedRequest.

    Returns ``None`` when the subject does not contain the ``[vellum]`` tag,
    signalling that the email should be ignored entirely. Otherwise returns a
    ParsedRequest whose ``matter_number`` may be ``None`` and whose
    ``document_types`` may be empty, both of which downstream code handles as
    branded error replies.
    """
    if not subject_is_vellum_request(subject):
        logger.info("email ignored: subject missing [vellum] tag", extra={"step": "parser.gate"})
        return None

    await _rate_limiter.acquire()

    client = _get_client()
    combined = f"Subject: {subject or ''}\n\nBody:\n{body or ''}"
    prompt = PROMPT_TEMPLATE.format(email_body=combined)

    response = await client.chat.completions.create(
        model=config.OPENAI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content or "{}"

    try:
        raw = json.loads(_strip_code_fences(content))
    except json.JSONDecodeError:
        logger.error(
            "parser failed to decode model response",
            extra={"step": "parser.decode", "raw_response": content},
        )
        raw = {"matter_number": None, "document_types": []}

    parsed = _normalise(raw, request_id, sender_email)
    logger.info(
        "email parsed",
        extra={
            "step": "parser.parse",
            "matter_number": parsed.matter_number,
            "document_types": parsed.document_types,
        },
    )
    return parsed
