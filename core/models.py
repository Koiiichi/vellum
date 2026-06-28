"""Pydantic models shared across the Vellum pipeline.

These models define the contracts passed between the listener, parser,
scraper, packager, summarizer and mailer. ``VALID_DOC_TYPES`` is the single
authoritative set of document types recognised anywhere in the system.
"""

from pydantic import BaseModel

VALID_DOC_TYPES = {
    "Exhibits",
    "Key Documents",
    "Other Documents",
    "Transcripts",
    "Recordings",
}


class ParsedRequest(BaseModel):
    """A single user request extracted from an inbound email."""

    request_id: str
    sender_email: str
    matter_number: str | None
    document_types: list[str]


class MatterMetadata(BaseModel):
    """Metadata extracted from a UARB matter page plus per-job download stats."""

    matter_number: str
    title: str
    matter_type: str
    status: str
    category: str
    date_received: str
    date_final_submissions: str
    outcome: str | None
    tab_counts: dict[str, int]
    requested_types: list[str]
    downloaded: dict[str, int]
    total_per_requested_tab: dict[str, int]


class JobResult(BaseModel):
    """Outcome of processing a single job through the worker pipeline."""

    request_id: str
    matter_number: str
    document_types: list[str]
    success: bool
    files_downloaded: dict[str, int]
    error: str | None
    duration_seconds: float
