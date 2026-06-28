"""Vellum entry point.

Two modes:

* ``--dry-run`` runs the full scrape, packages the ZIP and job_summary.json, and
  renders the branded email body to stdout without sending anything. It bypasses
  the Gmail listener and constructs the request directly from CLI arguments.
* Default (serve) mode starts the FastAPI webhook listener, the asyncio worker
  pool, and the Gmail watch renewal loop together in one event loop.

Examples:

    python main.py --dry-run --matter M12205 --types "Other Documents"
    python main.py --dry-run --matter M12205 --types "Exhibits,Transcripts"
    python main.py --dry-run --matter M12205 --types all
    python main.py
"""

import argparse
import asyncio
import sys

import uvicorn

from agent.summarizer import DISPLAY_ORDER
from core import config
from agent.gmail_watch import watch_renewal_loop
from core.logger import configure_logging, get_logger, new_request_id
from core.models import VALID_DOC_TYPES, ParsedRequest
from core.queue import process_request, start_workers

logger = get_logger(__name__)

_CANONICAL_TYPES = {t.lower(): t for t in VALID_DOC_TYPES}


def _resolve_types(types_arg: str) -> list[str]:
    """Expand and validate the --types argument into canonical type names."""
    if types_arg.strip().lower() == "all":
        return list(DISPLAY_ORDER)

    resolved: list[str] = []
    for raw in types_arg.split(","):
        name = raw.strip()
        if not name:
            continue
        canonical = _CANONICAL_TYPES.get(name.lower())
        if canonical is None:
            valid = ", ".join(sorted(VALID_DOC_TYPES))
            raise SystemExit(f"Unknown document type '{name}'. Valid types: {valid}")
        if canonical not in resolved:
            resolved.append(canonical)
    if not resolved:
        raise SystemExit("No valid document types provided.")
    return resolved


def _run_dry_run(matter: str, types_arg: str) -> None:
    """Build a request from CLI arguments and run the pipeline without sending."""
    document_types = _resolve_types(types_arg)
    parsed = ParsedRequest(
        request_id=new_request_id(),
        sender_email=config.EMAIL_FROM or "dry-run@vellum.local",
        matter_number=matter.strip().upper(),
        document_types=document_types,
    )
    asyncio.run(process_request(parsed, dry_run=True))


async def _serve() -> None:
    """Start the webhook listener, worker pool, and watch renewal together."""
    from agent.listener import create_app, set_history_id
    from agent.gmail_watch import renew_watch

    watch_result = await asyncio.to_thread(renew_watch)
    set_history_id(watch_result.get("historyId"))

    queue: asyncio.Queue[ParsedRequest] = asyncio.Queue()
    app = create_app(queue)
    workers = start_workers(queue)
    renewal = asyncio.create_task(watch_renewal_loop())

    server = uvicorn.Server(
        uvicorn.Config(app, host=config.HOST, port=config.PORT, log_config=None)
    )

    logger.info(
        f"vellum serving on {config.HOST}:{config.PORT}",
        extra={"step": "main.serve", "workers": len(workers)},
    )
    await asyncio.gather(server.serve(), renewal, *workers)


def main() -> None:
    """Parse CLI arguments and dispatch to dry-run or serve mode."""
    configure_logging()

    arg_parser = argparse.ArgumentParser(prog="vellum", description="Vellum regulatory filing agent.")
    arg_parser.add_argument("--dry-run", action="store_true", help="Scrape and render without sending email.")
    arg_parser.add_argument("--matter", help="Matter number, e.g. M12205 (dry-run only).")
    arg_parser.add_argument("--types", help="Comma-separated document types, or 'all' (dry-run only).")
    args = arg_parser.parse_args()

    if args.dry_run:
        if not args.matter or not args.types:
            arg_parser.error("--dry-run requires both --matter and --types")
        _run_dry_run(args.matter, args.types)
        return

    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        print("shutting down", file=sys.stderr)


if __name__ == "__main__":
    main()
