"""Cloud Storage delivery for archives too large to attach to email.

When a built archive exceeds the email provider's attachment limit, it is
uploaded to a Cloud Storage bucket and the recipient receives a download link
instead of an attachment. Uploaded objects are keyed by request id so each
delivery is isolated.
"""

from pathlib import Path

from google.cloud import storage

from core import config
from core.logger import get_logger

logger = get_logger(__name__)

_client: storage.Client | None = None


def _get_client() -> storage.Client:
    """Return a lazily constructed shared Cloud Storage client."""
    global _client
    if _client is None:
        _client = storage.Client()
    return _client


def upload_archive(path: Path, request_id: str) -> str:
    """Upload an archive to the delivery bucket and return its download URL."""
    path = Path(path)
    object_name = f"deliveries/{request_id}/{path.name}"
    bucket = _get_client().bucket(config.GCS_BUCKET)
    blob = bucket.blob(object_name)
    blob.upload_from_filename(str(path), content_type="application/zip")

    url = f"https://storage.googleapis.com/{config.GCS_BUCKET}/{object_name}"
    logger.info(
        "archive uploaded to cloud storage",
        extra={"step": "storage.upload", "url": url},
    )
    return url
