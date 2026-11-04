"""
Background-job monitoring.

* Failed and retried Celery tasks are logged to stderr (the host's log), with the task name, id and traceback.
* Beat runs `apps.core.tasks.heartbeat` every minute; it stamps the cache. GET /api/v1/health/jobs/ (apps/core/health.py) turns that
  stamp, the broker queue length and the unprocessed Stripe receipts into one ok / unhealthy answer, so an
  uptime pinger can alert when the worker or beat stops, or when paid checkouts are not becoming orders.
"""
import logging
import time
from datetime import timedelta

from celery.signals import task_failure, task_retry
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger("tss.jobs")

HEARTBEAT_KEY = "jobs:heartbeat"
# Beat fires the heartbeat every 60s; three missed beats means beat or the worker is down
HEARTBEAT_MAX_AGE = 180
# A receipt is stored before Stripe gets its 2xx and the sweeper retries it every 10s, so older means stuck
RECEIPT_MAX_AGE = timedelta(minutes=5)


@task_failure.connect
def log_task_failure(sender=None, task_id=None, exception=None, args=None, kwargs=None, einfo=None, **_):
    logger.error(
        "Task %s [%s] failed: %r args=%r kwargs=%r\n%s",
        getattr(sender, "name", sender), task_id, exception, args, kwargs, einfo,
    )


@task_retry.connect
def log_task_retry(sender=None, request=None, reason=None, **_):
    logger.warning(
        "Task %s [%s] will retry (attempt %s): %s",
        getattr(sender, "name", sender), getattr(request, "id", None), getattr(request, "retries", 0) + 1, reason,
    )


def stamp_heartbeat():
    cache.set(HEARTBEAT_KEY, time.time(), None)


def queue_length():
    """Tasks waiting in the Redis broker queue, or None when there is no Redis broker to ask."""
    from config.celery import app

    if settings.CELERY_TASK_ALWAYS_EAGER or not str(settings.CELERY_BROKER_URL).startswith(("redis://", "rediss://")):
        return None
    try:
        with app.connection_for_read() as connection:
            return int(connection.default_channel.client.llen("celery"))
    except Exception:
        logger.exception("Could not read the Celery queue length")
        return None


def job_health():
    """The checks behind the health endpoint: {"status": "ok"|"unhealthy", "problems": [...], ...}."""
    from apps.webhooks.models import FAILED, RECEIVED, IncomingWebhookReceipt

    problems = []

    beat_at = cache.get(HEARTBEAT_KEY)
    heartbeat_age = None if beat_at is None else round(time.time() - beat_at)
    if settings.CELERY_TASK_ALWAYS_EAGER:
        heartbeat_age = None  # tasks run inline: there is no worker or beat to watch
    elif heartbeat_age is None or heartbeat_age > HEARTBEAT_MAX_AGE:
        problems.append("No heartbeat from beat and the worker (is either one down?)")

    queued = queue_length()
    limit = settings.JOBS_QUEUE_ALERT_LENGTH
    if queued is not None and queued > limit:
        problems.append(f"{queued} tasks are waiting in the queue (limit {limit})")

    receipts = IncomingWebhookReceipt.objects
    stuck = receipts.filter(status=RECEIVED, received_at__lt=timezone.now() - RECEIPT_MAX_AGE).count()
    failed = receipts.filter(status=FAILED).count()
    if stuck:
        problems.append(f"{stuck} Stripe events received over {int(RECEIPT_MAX_AGE.total_seconds() // 60)} minutes ago are not processed")
    if failed:
        problems.append(f"{failed} Stripe events failed permanently")

    return {
        "status": "unhealthy" if problems else "ok",
        "problems": problems,
        "heartbeatAgeSeconds": heartbeat_age,
        "queueLength": queued,
        "stuckStripeEvents": stuck,
        "failedStripeEvents": failed,
    }
