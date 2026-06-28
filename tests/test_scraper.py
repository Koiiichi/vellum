"""Integration test for the scraper against the live UARB portal.

This test drives a real Chromium session against the live site and is therefore
skipped unless ``VELLUM_LIVE_TESTS=1`` is set in the environment. It validates
the navigation sequence, metadata extraction, tab-count parsing, and download
interception end to end using the reference matter M12205.
"""

import os
from pathlib import Path

import pytest

from agent import scraper

pytestmark = pytest.mark.skipif(
    os.getenv("VELLUM_LIVE_TESTS") != "1",
    reason="live UARB integration test; set VELLUM_LIVE_TESTS=1 to run",
)

REFERENCE_MATTER = "M12205"


async def test_scrape_reference_matter_exhibits(tmp_path: Path):
    result = await scraper.run(REFERENCE_MATTER, ["Exhibits"], tmp_path)

    assert result.metadata is not None
    metadata = result.metadata
    assert metadata.matter_number == REFERENCE_MATTER
    assert metadata.title
    assert metadata.tab_counts.get("Exhibits", 0) > 0

    downloaded = result.files_by_type.get("Exhibits", [])
    assert len(downloaded) > 0
    assert len(downloaded) <= scraper.config.MAX_DOCUMENTS
    for path in downloaded:
        assert path.exists()
        assert path.stat().st_size > 0


async def test_scrape_unknown_matter_raises(tmp_path: Path):
    with pytest.raises(scraper.MatterNotFoundError):
        await scraper.run("M00000", ["Exhibits"], tmp_path)
