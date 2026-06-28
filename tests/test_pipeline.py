"""Tests for the packaging and summarizing stages.

These exercise the deterministic, offline parts of the pipeline: ZIP layout,
job_summary.json contents, subject line composition, and template rendering.
"""

import json
import zipfile
from datetime import datetime, timezone

import pytest

from agent import packager, summarizer
from agent.scraper import ScrapeResult
from core.models import VALID_DOC_TYPES, MatterMetadata


def _metadata(requested, downloaded, totals) -> MatterMetadata:
    return MatterMetadata(
        matter_number="M12205",
        title="Halifax Regional Water Commission - Windsor Street Exchange Redevelopment Project - $69,275,000",
        matter_type="Capital Expenditure Approvals",
        status="Awaiting Compliance",
        category="Water",
        date_received="04/07/2025",
        date_final_submissions="10/23/2025",
        outcome=None,
        tab_counts={
            "Exhibits": 13,
            "Key Documents": 5,
            "Other Documents": 42,
            "Transcripts": 0,
            "Recordings": 0,
        },
        requested_types=requested,
        downloaded=downloaded,
        total_per_requested_tab=totals,
    )


def test_zip_filename_single():
    name = packager.build_zip_filename("M12205", ["Other Documents"], "2026-06-26")
    assert name == "vellum_M12205_OtherDocuments_2026-06-26.zip"


def test_zip_filename_multi():
    name = packager.build_zip_filename("M12205", ["Exhibits", "Transcripts"], "2026-06-26")
    assert name == "vellum_M12205_Exhibits+Transcripts_2026-06-26.zip"


def test_zip_filename_all():
    name = packager.build_zip_filename("M12205", list(VALID_DOC_TYPES), "2026-06-26")
    assert name == "vellum_M12205_AllTypes_2026-06-26.zip"


def test_build_zip_structure_and_summary(tmp_path):
    exhibit_dir = tmp_path / "Exhibits"
    exhibit_dir.mkdir()
    file_a = exhibit_dir / "98234_Application.pdf"
    file_b = exhibit_dir / "98235_Schedule_A.pdf"
    file_a.write_bytes(b"a")
    file_b.write_bytes(b"b")

    metadata = _metadata(
        requested=["Exhibits", "Transcripts"],
        downloaded={"Exhibits": 2, "Transcripts": 0},
        totals={"Exhibits": 13, "Transcripts": 0},
    )
    result = ScrapeResult(
        files_by_type={"Exhibits": [file_a, file_b], "Transcripts": []},
        metadata=metadata,
    )

    zip_path = packager.build(result, "req-123", tmp_path / "out")
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert zip_path.name == f"vellum_M12205_Exhibits+Transcripts_{day}.zip"

    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
        assert "Exhibits/98234_Application.pdf" in names
        assert "Exhibits/98235_Schedule_A.pdf" in names
        assert "Transcripts/" in names
        assert "job_summary.json" in names

        summary = json.loads(archive.read("job_summary.json"))

    assert summary["generated_by"] == "vellum"
    assert summary["request_id"] == "req-123"
    assert summary["matter_number"] == "M12205"
    assert summary["status"] == "Awaiting Compliance"
    assert summary["category"] == "Water"
    assert summary["downloaded"] == {"Exhibits": 2, "Transcripts": 0}
    assert summary["files"]["Exhibits"] == ["98234_Application.pdf", "98235_Schedule_A.pdf"]
    assert summary["files"]["Transcripts"] == []


def test_build_subject_single():
    metadata = _metadata(["Other Documents"], {"Other Documents": 10}, {"Other Documents": 42})
    assert summarizer.build_subject(metadata) == "[Vellum] M12205 \u00b7 10 Other Documents"


def test_build_subject_multi():
    metadata = _metadata(["Exhibits", "Transcripts"], {"Exhibits": 10, "Transcripts": 0}, {})
    assert summarizer.build_subject(metadata) == "[Vellum] M12205 \u00b7 Exhibits + Transcripts"


def test_build_subject_all():
    requested = list(VALID_DOC_TYPES)
    metadata = _metadata(requested, {t: 0 for t in requested}, {})
    assert summarizer.build_subject(metadata) == "[Vellum] M12205 \u00b7 All Document Types"


def test_render_success_contains_summary_lines():
    metadata = _metadata(
        ["Exhibits", "Transcripts"],
        {"Exhibits": 10, "Transcripts": 0},
        {"Exhibits": 13, "Transcripts": 0},
    )
    html = summarizer.render_success(metadata)
    assert "M12205" in html
    assert "Awaiting Compliance" in html
    assert "Downloaded 10 of 13 Exhibits" in html
    assert "No Transcripts found for this matter" in html
    assert "github.com/Koiiichi/vellum" in html


def test_render_error_matter_not_found():
    html = summarizer.render_error("matter_not_found", {"matter_number": "M99999"})
    assert "Matter not found" in html
    assert "M99999" in html


@pytest.mark.parametrize("code", ["no_matter_number", "no_document_types", "all_tabs_empty", "scrape_failed"])
def test_render_error_all_codes(code):
    html = summarizer.render_error(code, {"matter_number": "M12205"})
    assert "vellum" in html.lower()
    assert "{{" not in html
