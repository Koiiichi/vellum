"""Metadata-driven email body rendering and subject line composition.

All user-facing copy lives in the HTML templates under ``templates/``; this
module is the only place that maps a :class:`MatterMetadata` model (or an error
code) onto those templates. Business logic elsewhere never formats email copy
directly.
"""

import html
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from core import config
from core.models import VALID_DOC_TYPES, MatterMetadata

_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates"

DISPLAY_ORDER = [
    "Exhibits",
    "Key Documents",
    "Other Documents",
    "Transcripts",
    "Recordings",
]

_ERROR_COPY = {
    "no_matter_number": (
        "We could not find a matter number",
        "Your request did not contain a recognisable matter number. Matter "
        "numbers look like the letter M followed by five digits, for example "
        "M12205.",
    ),
    "no_document_types": (
        "Please specify a document type",
        "We found your matter number but could not tell which documents you "
        "need. Reply with one or more of the supported document types and we "
        "will pull them for you.",
    ),
    "matter_not_found": (
        "Matter not found",
        "We searched the UARB portal but no matter matched the number you "
        "provided. Please double-check the matter number and try again.",
    ),
    "all_tabs_empty": (
        "No documents available",
        "The matter exists, but none of the document types you requested "
        "contain any filings yet.",
    ),
    "scrape_failed": (
        "Something went wrong",
        "We hit an unexpected problem while retrieving your documents. The "
        "issue has been logged and you are welcome to try again shortly.",
    ),
}


@lru_cache(maxsize=None)
def _load_template(name: str) -> str:
    """Load and cache a raw HTML template by filename."""
    return (_TEMPLATE_DIR / name).read_text(encoding="utf-8")


def _fill(template: str, tokens: dict[str, str]) -> str:
    """Replace ``{{TOKEN}}`` placeholders with their rendered values."""
    rendered = template
    for key, value in tokens.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered


def _esc(value: str | None) -> str:
    """HTML-escape a value, rendering empty values as an em dash."""
    if value is None or value == "":
        return "&mdash;"
    return html.escape(value)


def _tab_breakdown_rows(metadata: MatterMetadata) -> str:
    """Render one row per document type with its count, highlighting requested."""
    rows = []
    for doc_type in DISPLAY_ORDER:
        count = metadata.tab_counts.get(doc_type, 0)
        requested = doc_type in metadata.requested_types
        weight = "600" if requested else "400"
        color = "#111111" if requested else "#6b7280"
        marker = " &nbsp;&middot;&nbsp; requested" if requested else ""
        rows.append(
            f'<tr>'
            f'<td style="padding:5px 12px 5px 0; font-weight:{weight}; color:{color}; white-space:nowrap;">'
            f"{html.escape(doc_type)}{marker}</td>"
            f'<td style="padding:5px 0; text-align:right; font-weight:{weight}; '
            f'color:{color}; white-space:nowrap; width:32px;">{count}</td>'
            f'</tr>'
        )
    return "\n".join(rows)


def _download_summary_rows(metadata: MatterMetadata) -> str:
    """Render one summary line per requested type."""
    rows = []
    for doc_type in metadata.requested_types:
        downloaded = metadata.downloaded.get(doc_type, 0)
        total = metadata.total_per_requested_tab.get(doc_type, 0)
        if downloaded > 0:
            text = f"Downloaded {downloaded} of {total} {html.escape(doc_type)}"
        else:
            text = f"No {html.escape(doc_type)} found for this matter"
        rows.append(
            f'<tr><td style="padding:5px 0; color:#111111;">{text}</td></tr>'
        )
    return "\n".join(rows)


def build_subject(metadata: MatterMetadata) -> str:
    """Compose the success subject line for single, multi, and all-type requests."""
    matter = metadata.matter_number
    requested = metadata.requested_types
    if set(requested) >= VALID_DOC_TYPES:
        return f"[Vellum] {matter} \u00b7 All Document Types"
    if len(requested) == 1:
        doc_type = requested[0]
        count = metadata.downloaded.get(doc_type, 0)
        return f"[Vellum] {matter} \u00b7 {count} {doc_type}"
    return f"[Vellum] {matter} \u00b7 {' + '.join(requested)}"


def build_error_subject(matter_number: str | None) -> str:
    """Compose the subject line for an error reply."""
    if matter_number:
        return f"[Vellum] {matter_number} \u00b7 Request could not be completed"
    return "[Vellum] Request could not be completed"


def _english_join(items: list[str], conjunction: str = "and") -> str:
    """Join a list into an English series with an Oxford comma."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} {conjunction} {items[1]}"
    return ", ".join(items[:-1]) + f", {conjunction} {items[-1]}"


def _format_date(value: str | None) -> str:
    """Render an MM/DD/YYYY date as a long-form date, falling back to raw input."""
    if not value:
        return ""
    try:
        parsed = datetime.strptime(value.strip(), "%m/%d/%Y")
    except (ValueError, TypeError):
        return value
    return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"


def _counts_sentence(metadata: MatterMetadata) -> str:
    """Compose a sentence describing how many documents exist per type."""
    nonzero: list[str] = []
    zero: list[str] = []
    for doc_type in DISPLAY_ORDER:
        count = metadata.tab_counts.get(doc_type, 0)
        if count > 0:
            nonzero.append(f"{count} {doc_type}")
        else:
            zero.append(doc_type)

    series = list(nonzero)
    if zero:
        series.append("no " + _english_join(zero, "or"))
    if not series:
        return "I did not find any documents on file for this matter"
    return "I found " + _english_join(series)


def _download_sentence(metadata: MatterMetadata, download_url: str | None = None) -> str:
    """Compose a sentence describing what was downloaded and how to retrieve it."""
    clauses: list[str] = []
    for doc_type in metadata.requested_types:
        downloaded = metadata.downloaded.get(doc_type, 0)
        total = metadata.total_per_requested_tab.get(doc_type, 0)
        if downloaded > 0:
            clauses.append(f"{downloaded} of the {total} {doc_type}")
    if not clauses:
        return (
            "None of the document types you requested contained downloadable "
            "filings yet"
        )
    if download_url:
        return (
            "I downloaded "
            + _english_join(clauses)
            + " and packaged them as a ZIP you can download using the button below"
        )
    return (
        "I downloaded "
        + _english_join(clauses)
        + " and have attached them as a ZIP here"
    )


def build_prose_summary(metadata: MatterMetadata, download_url: str | None = None) -> str:
    """Compose a conversational, HTML-safe summary paragraph for the matter.

    The greeting is static markup; all metadata-derived text is escaped before
    being combined with it.
    """
    matter = metadata.matter_number
    sentences: list[str] = []

    if metadata.title:
        sentences.append(f"{matter} is about {metadata.title}.")
    else:
        sentences.append(f"Here is what I found for matter {matter}.")

    if metadata.matter_type and metadata.category:
        sentences.append(
            f"It relates to {metadata.matter_type} within the "
            f"{metadata.category} category."
        )
    elif metadata.matter_type:
        sentences.append(f"It relates to {metadata.matter_type}.")
    elif metadata.category:
        sentences.append(f"It falls within the {metadata.category} category.")

    received = _format_date(metadata.date_received)
    final = _format_date(metadata.date_final_submissions)
    if received and final:
        sentences.append(
            f"The matter had an initial filing on {received} and a final "
            f"filing on {final}."
        )
    elif received:
        sentences.append(f"The matter had an initial filing on {received}.")

    sentences.append(_counts_sentence(metadata) + ".")
    sentences.append(_download_sentence(metadata, download_url) + ".")

    body = " ".join(sentences)
    return "Hi there,<br><br>" + html.escape(body)


def _logo_html() -> str:
    """Render the header logo as a hosted image, falling back to a text wordmark.

    Email clients render inline SVG inconsistently, so a hosted PNG referenced
    via ``<img>`` is used when a logo URL is configured.
    """
    url = config.EMAIL_LOGO_URL
    if url:
        safe = html.escape(url, quote=True)
        return (
            f'<img id="vellum-logo" src="{safe}" width="160" height="46" alt="Vellum" '
            'style="display:block; border:0; outline:none; text-decoration:none; '
            'width:160px; max-width:100%; height:auto;">'
        )
    return (
        '<span style="font-size:25px; font-weight:500; letter-spacing:0.03em; '
        'color:#111111;">vellum</span>'
    )


def _logo_dark_style() -> str:
    """Emit a prefers-color-scheme media query that swaps in the dark logo."""
    dark_url = config.EMAIL_LOGO_DARK_URL
    if not dark_url or not config.EMAIL_LOGO_URL:
        return ""
    safe = html.escape(dark_url, quote=True)
    return (
        '<style type="text/css">'
        "@media (prefers-color-scheme: dark) {"
        f'#vellum-logo {{ content: url("{safe}") !important; }}'
        "}"
        "</style>"
    )


def _download_link_block(download_url: str | None) -> str:
    """Render a download button row, or an empty string when attaching."""
    if not download_url:
        return ""
    safe_url = html.escape(download_url, quote=True)
    days = config.DELIVERY_RETENTION_DAYS
    return (
        '<tr><td style="padding:4px 8px 24px 8px;">'
        f'<a href="{safe_url}" '
        'style="display:inline-block; background-color:#111111; color:#ffffff; '
        "text-decoration:none; font-size:14px; font-weight:500; padding:12px 24px; "
        'border-radius:8px;">Download ZIP</a>'
        '<div style="padding:8px 0 0 0; font-size:12px; color:#6b7280;">'
        f"This link will remain available for {days} days.</div>"
        "</td></tr>"
    )



def render_success(metadata: MatterMetadata, download_url: str | None = None) -> str:
    """Render the branded success email body for a completed scrape.

    When ``download_url`` is provided the archive is offered as a download link
    rather than an attachment, for archives too large to attach to email.
    """
    template = _load_template("email_success.html")
    tokens = {
        "LOGO": _logo_html(),
        "LOGO_DARK_STYLE": _logo_dark_style(),
        "PROSE_SUMMARY": build_prose_summary(metadata, download_url),
        "DOWNLOAD_LINK": _download_link_block(download_url),
        "MATTER_NUMBER": _esc(metadata.matter_number),
        "TITLE": _esc(metadata.title),
        "MATTER_TYPE": _esc(metadata.matter_type),
        "CATEGORY": _esc(metadata.category),
        "STATUS": _esc(metadata.status),
        "DATE_RECEIVED": _esc(metadata.date_received),
        "DATE_FINAL": _esc(metadata.date_final_submissions),
        "OUTCOME": _esc(metadata.outcome),
        "TAB_BREAKDOWN_ROWS": _tab_breakdown_rows(metadata),
        "DOWNLOAD_SUMMARY_ROWS": _download_summary_rows(metadata),
    }
    return _fill(template, tokens)


def render_error(error_code: str, context: dict | None = None) -> str:
    """Render the branded error email body for a failed or unparseable request."""
    context = context or {}
    headline, body = _ERROR_COPY.get(error_code, _ERROR_COPY["scrape_failed"])

    matter_number = context.get("matter_number")
    requested = context.get("requested_types") or []
    detail_parts = []
    if matter_number:
        detail_parts.append(
            f"Matter: <span style=\"font-family:'SFMono-Regular', ui-monospace, "
            f'Menlo, Consolas, monospace;">{html.escape(str(matter_number))}</span>'
        )
    if requested:
        joined = ", ".join(html.escape(str(t)) for t in requested)
        detail_parts.append(f"Requested: {joined}")
    if context.get("detail"):
        detail_parts.append(html.escape(str(context["detail"])))
    detail = "<br>".join(detail_parts) if detail_parts else "No additional details."

    template = _load_template("email_error.html")
    tokens = {
        "LOGO": _logo_html(),
        "LOGO_DARK_STYLE": _logo_dark_style(),
        "ERROR_HEADLINE": html.escape(headline),
        "ERROR_BODY": html.escape(body),
        "ERROR_DETAIL": detail,
    }
    return _fill(template, tokens)
