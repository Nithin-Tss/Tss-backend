"""
RequestLogMiddleware: gives every request an id and writes one access-log line per /api/ request.

The id is the client's X-Request-ID when it is a sane value (so a proxy or the frontend can set one), else a
new UUID. It is returned in the response header and attached to every log line made while handling the request.
The query string is not logged: it can carry tokens.
"""
import logging
import re
import time
import uuid

from django.conf import settings

from .logging_utils import request_id_var

access_log = logging.getLogger("tss.access")

API_PREFIX = "/api/"
VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class RequestLogMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        sent = request.headers.get("X-Request-ID", "")
        request_id = sent if VALID_REQUEST_ID.match(sent) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.monotonic()
        status = 500  # what the client sees if the view raises and nothing else answers
        try:
            response = self.get_response(request)
            status = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            if request.path_info.startswith(API_PREFIX):
                self.log(request, status, (time.monotonic() - started) * 1000)
            request_id_var.reset(token)

    @staticmethod
    def log(request, status, duration_ms):
        slow = duration_ms >= settings.LOG_SLOW_REQUEST_SECONDS * 1000
        level = logging.ERROR if status >= 500 else logging.WARNING if slow else logging.INFO
        access_log.log(
            level, "%s %s %s %dms%s", request.method, request.path_info, status, duration_ms, " SLOW" if slow else "",
            extra={"method": request.method, "path": request.path_info, "status": status, "durationMs": round(duration_ms)},
        )
