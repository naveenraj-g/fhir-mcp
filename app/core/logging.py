"""
Structured logging — same conventions as fhir-gql/fhir-server:

- One JSON object per line (`logging.format: json`) or a flat human line
  (`console`), driven by configs/config.yaml's `logging:` block.
- Structured data goes in `extra={...}`; every line carries an `event` key
  (dotted, past tense: `tool.called`, `discovery.loaded`).
- Correlation fields come from ContextVars set by
  app.middleware.ToolCallLoggingMiddleware: `request_id` and the caller's
  `actor_sub` / `actor_org_id`. The `actor_` prefix is deliberate — FHIR
  payloads carry their own ids, so a bare `org_id` would be ambiguous.

FastMCP and Uvicorn install their own handlers (FastMCP's doesn't propagate
to root); setup_logging() removes them and routes both through ours, so the
stream stays parseable.
"""

import json
import logging
import sys
from contextvars import ContextVar
from typing import Any

from app.core.config import LogConfig

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
actor_sub_var: ContextVar[str | None] = ContextVar("actor_sub", default=None)
actor_org_var: ContextVar[str | None] = ContextVar("actor_org_id", default=None)

_REDACTED = "***REDACTED***"

# LogRecord bookkeeping attributes — never promoted to output fields.
_SKIP = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime", "color_message",
}

# Loggers that ship their own handlers; reset so they flow through root.
_THIRD_PARTY = ("fastmcp", "mcp", "uvicorn", "uvicorn.error", "uvicorn.access", "httpx2", "httpcore2")


def log_context() -> dict[str, str]:
    ctx = {
        "request_id": request_id_var.get(),
        "actor_sub": actor_sub_var.get(),
        "actor_org_id": actor_org_var.get(),
    }
    return {k: v for k, v in ctx.items() if v}


def _extras(record: logging.LogRecord, exclude: set[str]) -> dict[str, Any]:
    return {k: v for k, v in record.__dict__.items() if k not in _SKIP and k not in exclude}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        out.update(log_context())
        out.update(_extras(record, set(out)))
        if record.exc_info:
            out["traceback"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


class ConsoleFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-8s %(name)s | %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        ctx = log_context()
        prefix = f"[{ctx['request_id'][:8]}] " if "request_id" in ctx else ""
        extras = _extras(record, set(ctx))
        if record.exc_info:
            extras["traceback"] = self.formatException(record.exc_info)
        suffix = (" " + " ".join(f"{k}={v!r}" for k, v in extras.items())) if extras else ""
        return f"{prefix}{base}{suffix}"


def setup_logging(config: LogConfig) -> None:
    """Configure the root logger. Idempotent — replaces handlers rather than
    stacking another one."""
    handler = logging.StreamHandler(sys.stderr)  # stdout is the stdio transport's wire
    handler.setFormatter(JsonFormatter() if config.format == "json" else ConsoleFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(config.level)

    for name in _THIRD_PARTY:
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    # The HTTP stack logs every request at INFO; our middleware already does.
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    logging.getLogger("httpcore2").setLevel(logging.WARNING)


def redact(value: Any, keys: list[str]) -> Any:
    """Mask any dict key containing one of `keys` (case-insensitive), at any
    depth. A backstop for secrets in debug payload logs — not a PHI filter."""
    lowered = [k.lower() for k in keys]
    if isinstance(value, dict):
        return {
            k: _REDACTED if any(s in str(k).lower() for s in lowered) else redact(v, keys)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v, keys) for v in value]
    return value


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
