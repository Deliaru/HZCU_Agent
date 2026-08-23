import json
import logging
import re
import time
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from hzcu_agent.config import Settings

request_id_context: ContextVar[str] = ContextVar("request_id", default="-")
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
EXTRA_FIELDS = (
    "event",
    "method",
    "path",
    "status_code",
    "duration_ms",
    "task_id",
    "answer_id",
    "tool",
    "trace_id",
    "evidence_count",
    "error_code",
    "role",
    "attempt",
    "output_length",
    "output_sha256",
    "error_types",
    "error_paths",
    "query_variants",
    "source_hints",
    "routed_sources",
    "retrieval_channels",
    "candidate_ranking",
    "deduplication",
    "coverage_risk",
)


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_context.get(),
        }
        for field in EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info and record.exc_info[0]:
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(settings: Settings) -> None:
    handler = logging.StreamHandler()
    if settings.log_format == "json":
        handler.setFormatter(JsonLogFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s [request_id=%(request_id)s] %(message)s"
            )
        )
        handler.addFilter(_RequestIdFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    for library in ("httpx", "httpcore", "sqlalchemy.engine"):
        logging.getLogger(library).setLevel(logging.WARNING)
    # The default Uvicorn access record includes the raw query string. CAS
    # callbacks contain a single-use ticket, so all request logging must go
    # through RequestContextMiddleware, which records only the sanitized path.
    logging.getLogger("uvicorn.access").disabled = True


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_context.get()
        return True


class RequestContextMiddleware:
    """Assign a safe correlation ID and log one sanitized record per HTTP request."""

    def __init__(self, app) -> None:
        self.app = app
        self._logger = logging.getLogger("hzcu_agent.http")

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming_id = _header_value(scope.get("headers", []), b"x-request-id")
        request_id = (
            incoming_id
            if incoming_id and REQUEST_ID_PATTERN.fullmatch(incoming_id)
            else f"req_{uuid4().hex}"
        )
        token = request_id_context.set(request_id)
        start = time.perf_counter()
        status_code = 500

        async def send_with_request_id(message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            self._logger.info(
                "HTTP request completed",
                extra={
                    "event": "http.request.completed",
                    "method": scope.get("method"),
                    "path": scope.get("path"),
                    "status_code": status_code,
                    "duration_ms": duration_ms,
                },
            )
            request_id_context.reset(token)


def _header_value(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    for key, value in headers:
        if key.lower() == name:
            try:
                return value.decode("ascii")
            except UnicodeDecodeError:
                return None
    return None
