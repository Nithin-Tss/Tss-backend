"""
Logging helpers (wired up in settings.LOGGING): a request id on every log line, and an optional JSON formatter.

The id is set per request by RequestLogMiddleware and returned to the client as X-Request-ID, so a user's error
report can be matched to the exact log lines. Outside a request (workers, management commands) it is "-".
"""
import json
import logging
from contextvars import ContextVar

request_id_var = ContextVar("request_id", default="-")

# Fields RequestLogMiddleware attaches to its access-log record; the JSON formatter copies them through
ACCESS_FIELDS = ("method", "path", "status", "durationMs")


class RequestIdFilter(logging.Filter):
    def filter(self, record):
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record):
        entry = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "requestId": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        entry.update({key: getattr(record, key) for key in ACCESS_FIELDS if hasattr(record, key)})
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)
