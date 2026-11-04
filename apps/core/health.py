"""
Health checks.

* GET /healthz  liveness: the process answers. No database call, so a slow database never gets the app restarted.
* GET /readyz   readiness: PostgreSQL and the cache respond; 503 otherwise. Point the host's health check here.
* GET /api/v1/health/jobs/  background jobs (apps/core/monitoring.py), for an uptime pinger.
"""
import hmac
import logging

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from .monitoring import job_health

logger = logging.getLogger(__name__)


class JobsHealthView(APIView):
    """
    GET /api/v1/health/jobs/ — 200 when background jobs are healthy, 503 with the problems when not.
    For an uptime pinger: send the JOBS_HEALTH_TOKEN env value as the X-Health-Token header (or sign in as a
    platform administrator). Without a configured token only platform administrators get an answer.
    """

    permission_classes = [AllowAny]  # checked by hand below: a pinger cannot sign in
    throttle_classes = []
    schema = None

    def get(self, request):
        token = settings.JOBS_HEALTH_TOKEN
        sent = request.headers.get("X-Health-Token", "")
        user = request.user
        allowed = bool(token) and hmac.compare_digest(sent.encode(), token.encode())
        allowed = allowed or bool(user and user.is_authenticated and getattr(user, "is_owner", False))
        if not allowed:
            return JsonResponse({"detail": "Not found."}, status=404)
        health = job_health()
        return JsonResponse(health, status=200 if health["status"] == "ok" else 503)


@require_GET
def healthz(request):
    return JsonResponse({"status": "ok"})


def _check(name, probe, failures):
    try:
        probe()
    except Exception:
        logger.exception("Readiness check failed: %s", name)
        failures.append(name)


def _probe_cache():
    cache.set("readyz", "1", 10)
    if cache.get("readyz") != "1":
        raise RuntimeError("cache did not return the value just stored")


@require_GET
def readyz(request):
    failures = []
    _check("database", lambda: connection.cursor().execute("SELECT 1"), failures)
    _check("cache", _probe_cache, failures)
    # Names only: the cause is in the log, not in a response anyone can read
    return JsonResponse(
        {"status": "unavailable" if failures else "ok", "failing": failures}, status=503 if failures else 200,
    )
