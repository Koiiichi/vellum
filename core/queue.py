"""Async job queue and worker pipeline.

A bounded pool of worker coroutines drains an ``asyncio.Queue`` of
:class:`ParsedRequest` jobs. Each job runs the full pipeline: scrape, package,
render, and reply. The number of workers bounds how many Playwright browser
sessions run concurrently. Typed scraper errors are mapped to branded error
replies; a per-type ``TabEmptyError`` is handled inside the scraper and never
fails the job.
"""

import asyncio
import tempfile
import time
from pathlib import Path

from agent import mailer, packager, scraper, storage, summarizer
from core import config
from core.logger import get_logger, set_request_id
from core.models import JobResult, ParsedRequest

logger = get_logger(__name__)


async def _deliver(
    to_email: str,
    subject: str,
    html_body: str,
    attachment: Path | None,
    dry_run: bool,
) -> None:
    """Send a reply, or print it to stdout when running in dry-run mode."""
    if dry_run:
        print("=" * 80)
        print(f"TO:      {to_email}")
        print(f"SUBJECT: {subject}")
        if attachment is not None:
            print(f"ATTACH:  {attachment}")
        print("-" * 80)
        print(html_body)
        print("=" * 80)
        return
    await mailer.send(to_email, subject, html_body, attachment)


async def _send_error(
    parsed: ParsedRequest,
    error_code: str,
    context: dict,
    dry_run: bool,
) -> None:
    """Render and deliver a branded error reply."""
    html_body = summarizer.render_error(error_code, context)
    subject = summarizer.build_error_subject(parsed.matter_number)
    await _deliver(parsed.sender_email, subject, html_body, None, dry_run)


async def process_request(parsed: ParsedRequest, dry_run: bool = False) -> JobResult:
    """Run a single parsed request through the full pipeline.

    Returns a :class:`JobResult` describing the outcome. Validation failures and
    typed scraper errors produce branded error replies rather than raising.
    """
    set_request_id(parsed.request_id)
    started = time.monotonic()

    def result(success: bool, files: dict[str, int], error: str | None) -> JobResult:
        return JobResult(
            request_id=parsed.request_id,
            matter_number=parsed.matter_number or "",
            document_types=parsed.document_types,
            success=success,
            files_downloaded=files,
            error=error,
            duration_seconds=round(time.monotonic() - started, 3),
        )

    if not parsed.matter_number:
        await _send_error(parsed, "no_matter_number", {}, dry_run)
        return result(False, {}, "no_matter_number")

    if not parsed.document_types:
        await _send_error(
            parsed,
            "no_document_types",
            {"matter_number": parsed.matter_number},
            dry_run,
        )
        return result(False, {}, "no_document_types")

    job_dir = Path(tempfile.mkdtemp(prefix=f"vellum_{parsed.request_id[:8]}_"))

    try:
        scrape_result = await scraper.run(
            parsed.matter_number, parsed.document_types, job_dir
        )
    except scraper.MatterNotFoundError as exc:
        logger.warning(f"matter not found: {exc}", extra={"step": "queue.scrape"})
        await _send_error(
            parsed, "matter_not_found", {"matter_number": parsed.matter_number}, dry_run
        )
        return result(False, {}, "matter_not_found")
    except scraper.ScraperError as exc:
        logger.exception("scrape failed", extra={"step": "queue.scrape"})
        await _send_error(
            parsed,
            "scrape_failed",
            {"matter_number": parsed.matter_number, "detail": str(exc)},
            dry_run,
        )
        return result(False, {}, "scrape_failed")

    metadata = scrape_result.metadata
    downloaded = dict(metadata.downloaded)

    if sum(downloaded.values()) == 0:
        await _send_error(
            parsed,
            "all_tabs_empty",
            {
                "matter_number": parsed.matter_number,
                "requested_types": parsed.document_types,
            },
            dry_run,
        )
        return result(False, downloaded, "all_tabs_empty")

    zip_path = packager.build(scrape_result, parsed.request_id, job_dir)

    download_url: str | None = None
    attachment: Path | None = zip_path
    size_mb = zip_path.stat().st_size / (1024 * 1024)
    if config.GCS_BUCKET and not dry_run and (config.RESEND_API_KEY or size_mb > config.MAX_ATTACHMENT_MB):
        download_url = await asyncio.to_thread(
            storage.upload_archive, zip_path, parsed.request_id
        )
        attachment = None
        logger.info(
            "sending archive as download link",
            extra={
                "step": "queue.deliver",
                "size_mb": round(size_mb, 1),
                "limit_mb": config.MAX_ATTACHMENT_MB,
                "resend_delivery": bool(config.RESEND_API_KEY),
            },
        )

    html_body = summarizer.render_success(metadata, download_url=download_url)
    subject = summarizer.build_subject(metadata)

    await _deliver(parsed.sender_email, subject, html_body, attachment, dry_run)

    logger.info(
        "job complete",
        extra={
            "step": "queue.complete",
            "matter_number": parsed.matter_number,
            "downloaded": downloaded,
            "zip_path": str(zip_path),
            "download_url": download_url,
            "dry_run": dry_run,
        },
    )
    return result(True, downloaded, None)


async def worker(queue: "asyncio.Queue[ParsedRequest]", worker_id: int) -> None:
    """Continuously drain the queue, processing one request at a time."""
    logger.info(f"worker {worker_id} started", extra={"step": "queue.worker"})
    while True:
        parsed = await queue.get()
        try:
            await process_request(parsed)
        except Exception:
            logger.exception("unhandled error in worker", extra={"step": "queue.worker"})
        finally:
            queue.task_done()


def start_workers(queue: "asyncio.Queue[ParsedRequest]", count: int | None = None) -> list[asyncio.Task]:
    """Spawn the worker pool and return the created tasks."""
    count = count or config.MAX_CONCURRENT_WORKERS
    return [asyncio.create_task(worker(queue, i + 1)) for i in range(count)]
