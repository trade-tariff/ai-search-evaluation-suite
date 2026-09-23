"""JSON logs for Amazon ECS, in the same Logstash shape as trade-tariff-backend.

Backend production logs use Lograge's Logstash formatter: one JSON object per
line, with @timestamp, @version, message, and request fields. CloudWatch
ingests container stdout, so this process must emit that shape and must not
emit load-balancer or container health probes.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

HEALTHCHECK_PATHS = ("/api/health", "/api/live", "/healthcheckz")

_CONFIGURED = False


def is_healthcheck_path(path: str) -> bool:
    bare = path.split("?", 1)[0]
    return bare in HEALTHCHECK_PATHS or bare.startswith("/api/health/")


def _path_from_record(record: logging.LogRecord) -> str:
    args = record.args
    if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
        return args[2].split("?", 1)[0]
    path = getattr(record, "path", None)
    if isinstance(path, str):
        return path.split("?", 1)[0]
    return ""


class DropHealthCheckFilter(logging.Filter):
    """Drop uvicorn access lines and app request logs for health probes."""

    def filter(self, record: logging.LogRecord) -> bool:
        if is_healthcheck_path(_path_from_record(record)):
            return False
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            return True
        return not any(
            token in message
            for token in (
                '"GET /api/health',
                '"GET /api/live',
                '"GET /healthcheckz',
                "GET /api/health ",
                "GET /api/live ",
            )
        )


class EcsJsonFormatter(logging.Formatter):
    """One Logstash-compatible JSON object per line."""

    _EXTRA_FIELDS = (
        "method",
        "path",
        "status",
        "duration",
        "event",
        "job_id",
        "run_id",
        "run_label",
        "harness",
        "returncode",
    )

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        payload: dict[str, object] = {
            "@timestamp": timestamp,
            "@version": "1",
            "message": record.getMessage(),
            "level": record.levelname,
            "logger": record.name,
        }
        args = record.args
        if record.name == "uvicorn.access" and isinstance(args, tuple) and len(args) >= 5:
            path = str(args[2]).split("?", 1)[0]
            payload["method"] = args[1]
            payload["path"] = path
            payload["status"] = args[4]
            payload["message"] = f"[{args[4]}] {args[1]} {path}"
        for key in self._EXTRA_FIELDS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception_message"] = self.formatException(record.exc_info)[:500]
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(EcsJsonFormatter())
    handler.addFilter(DropHealthCheckFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    root.addHandler(handler)

    for name in ("uvicorn", "uvicorn.error", "experiment", "ecs.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(logging.INFO)
        logger.addFilter(DropHealthCheckFilter())

    # Uvicorn's access logger is the source of /api/health lines. Disable it.
    # Real requests are logged by the app middleware, which skips those paths.
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True
    access.addFilter(DropHealthCheckFilter())

    _CONFIGURED = True
