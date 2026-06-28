"""Structured JSON logging with request-scoped context threading.

Every log line is emitted as a single JSON object. A ``request_id`` context
variable is threaded through every log call for the lifetime of a job so that
all lines for one email request share the same identifier and can be grepped
together. Additional structured fields (matter_number, document_types, step,
duration_ms, ...) are passed through the standard logging ``extra`` mechanism.
"""

import contextvars
import json
import logging
import sys
import uuid
from datetime import datetime, timezone

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)

_RESERVED_FIELDS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime",
}


def new_request_id() -> str:
    """Generate and bind a new request id for the current context."""
    request_id = str(uuid.uuid4())
    request_id_var.set(request_id)
    return request_id


def set_request_id(request_id: str) -> None:
    """Bind an existing request id to the current context."""
    request_id_var.set(request_id)


class JSONFormatter(logging.Formatter):
    """Format log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        """Render a log record into a JSON string."""
        payload: dict = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "level": record.levelname,
            "request_id": request_id_var.get(),
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key not in _RESERVED_FIELDS and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Install the JSON formatter on the root logger.

    Idempotent: repeated calls replace existing handlers so logging output
    stays consistent across module reloads and test runs.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger that inherits the configured root handler."""
    return logging.getLogger(name)
