"""ZIP packaging and job summary generation.

Builds the branded archive returned to the user: each requested document type
becomes a subfolder, empty requested types are preserved as empty folders, and a
``job_summary.json`` manifest is embedded at the archive root. The manifest is
the structured artifact a downstream ingestion pipeline consumes alongside the
raw files.
"""

import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from core.logger import get_logger
from core.models import VALID_DOC_TYPES, MatterMetadata
from agent.scraper import ScrapeResult

logger = get_logger(__name__)


def _type_label(doc_type: str) -> str:
    """Collapse a document type into a filename-safe token."""
    return doc_type.replace(" ", "")


def build_zip_filename(matter_number: str, requested_types: list[str], day: str) -> str:
    """Compose the archive filename for single, multi, and all-type requests."""
    if set(requested_types) >= VALID_DOC_TYPES:
        label = "AllTypes"
    elif len(requested_types) == 1:
        label = _type_label(requested_types[0])
    else:
        label = "+".join(_type_label(t) for t in requested_types)
    return f"vellum_{matter_number}_{label}_{day}.zip"


def build_summary(result: ScrapeResult, request_id: str) -> dict:
    """Assemble the job_summary.json manifest from a scrape result."""
    metadata: MatterMetadata = result.metadata
    files = {
        doc_type: [p.name for p in result.files_by_type.get(doc_type, [])]
        for doc_type in metadata.requested_types
    }
    return {
        "generated_by": "vellum",
        "request_id": request_id,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "matter_number": metadata.matter_number,
        "matter_title": metadata.title,
        "status": metadata.status,
        "category": metadata.category,
        "date_received": metadata.date_received,
        "date_final_submissions": metadata.date_final_submissions,
        "outcome": metadata.outcome,
        "tab_counts": metadata.tab_counts,
        "requested_types": metadata.requested_types,
        "downloaded": metadata.downloaded,
        "files": files,
    }


def build(result: ScrapeResult, request_id: str, dest_dir: Path) -> Path:
    """Build the ZIP archive and return its path.

    Each requested type is written to its own subfolder; requested types with no
    downloaded files are preserved as empty folders and noted in the manifest.
    """
    metadata: MatterMetadata = result.metadata
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    zip_name = build_zip_filename(metadata.matter_number, metadata.requested_types, day)
    zip_path = dest_dir / zip_name

    summary = build_summary(result, request_id)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for doc_type in metadata.requested_types:
            files = result.files_by_type.get(doc_type, [])
            if not files:
                archive.writestr(f"{doc_type}/", "")
                continue
            for file_path in files:
                archive.write(file_path, arcname=f"{doc_type}/{file_path.name}")
        archive.writestr("job_summary.json", json.dumps(summary, indent=2))

    logger.info(
        f"packaged archive: {zip_name}",
        extra={
            "step": "packager.build",
            "matter_number": metadata.matter_number,
            "zip_path": str(zip_path),
        },
    )
    return zip_path
