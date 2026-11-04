"""
Background jobs (run by Celery, see config/celery.py). The periodic ones wrap the same
services the management commands use, so cron and Celery stay interchangeable.
"""
import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.db import OperationalError
from django.utils import timezone

logger = logging.getLogger(__name__)


# --- Job tracking (orders.BackgroundJob): a row per run of a tracked task -------------------

def job_start(job_type, task, store_id=None, params=None):
    """
    Mark a run as started. `task` is the bound Celery task (self). A retry of the same Celery task
    (same request id) reuses its row and counts the retry; a plain call (no request id) gets a new row.
    """
    from apps.orders.models import BackgroundJob

    task_id = getattr(task.request, "id", None)
    values = {
        "job_type": job_type, "task_name": task.name, "store_id": store_id, "params": params or {},
        "status": BackgroundJob.RUNNING, "started_at": timezone.now(), "finished_at": None, "error": "",
        "retries": getattr(task.request, "retries", 0) or 0,
    }
    if task_id:
        return BackgroundJob.objects.update_or_create(celery_task_id=task_id, defaults=values)[0]
    return BackgroundJob.objects.create(**values)


def job_progress(job, percent):
    job.progress = min(max(int(percent), 0), 100)
    job.save(update_fields=["progress", "updated_at"])


def job_succeed(job, result=None):
    job.status, job.result, job.progress, job.finished_at = job.SUCCEEDED, result, 100, timezone.now()
    job.save(update_fields=["status", "result", "progress", "finished_at", "updated_at"])


def job_fail(job, error):
    job.status, job.error, job.finished_at = job.FAILED, f"{type(error).__name__}: {error}"[:5000], timezone.now()
    job.save(update_fields=["status", "error", "finished_at", "updated_at"])


@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_backoff_max=600, max_retries=5)
def send_email(self, subject, body, recipients):
    send_mail(subject, body, None, list(recipients))


def send_email_async(subject, body, recipients):
    """Queue an email. Inline (and raising on failure) when no broker is configured."""
    send_email.delay(subject, body, list(recipients))


@shared_task
def heartbeat():
    """Every minute from beat: proves beat and a worker are both alive (see apps/core/monitoring.py)."""
    from .monitoring import stamp_heartbeat

    stamp_heartbeat()


@shared_task
def deliver_webhooks(batch=50):
    from apps.webhooks import services

    return services.deliver_due(batch)


@shared_task
def expire_reservations():
    from apps.inventory import services

    return services.expire_reservations()


@shared_task
def sweep_carts(abandon_hours=1, expire_days=30):
    from apps.commerce import services
    from apps.tenancy.models import Store

    abandoned = expired = 0
    for store in Store.objects.filter(status="active"):
        abandoned += services.mark_abandoned(store, timedelta(hours=abandon_hours))
        expired += services.expire_carts(store, timedelta(days=expire_days))
    logger.info("Abandoned %s checkout(s), expired %s cart(s).", abandoned, expired)
    return abandoned, expired


@shared_task
def prune_audit_logs():
    from apps.audit.models import AuditLog

    return AuditLog.objects.purge_older_than(settings.AUDIT_LOG_RETENTION_DAYS)


@shared_task
def prune_webhook_deliveries():
    from apps.webhooks import services

    return services.prune_deliveries(settings.WEBHOOK_DELIVERY_RETENTION_DAYS)


@shared_task(bind=True, autoretry_for=(OperationalError,), retry_backoff=True, retry_backoff_max=600, max_retries=5)
def aggregate_daily_metrics(self, store_id=None, start=None, end=None, lookback_days=30):
    """
    Rebuild analytics.store_daily_metrics. With no dates, each store's last `lookback_days`
    local days up to today (so late refunds and cancellations are picked up); with ISO dates,
    that range (backfill). One store by id, or every active store. Safe to rerun or retry.
    """
    from datetime import date, timedelta

    from apps.core import reports
    from apps.tenancy.models import Store

    job = job_start("daily_metrics", self, store_id=store_id,
                    params={"start": start, "end": end, "lookback_days": lookback_days})
    try:
        stores = list(Store.objects.filter(pk=store_id) if store_id else Store.objects.filter(status="active"))
        written = 0
        for done, store in enumerate(stores, 1):
            last = date.fromisoformat(end) if end else reports.local_today(store)
            first = date.fromisoformat(start) if start else last - timedelta(days=lookback_days - 1)
            written += reports.aggregate_daily_metrics(store, first, last)
            job_progress(job, done * 100 // len(stores))
    except Exception as exc:
        job_fail(job, exc)
        raise
    job_succeed(job, result={"rows_written": written, "stores": len(stores)})
    logger.info("Wrote %s daily metric row(s).", written)
    return written
