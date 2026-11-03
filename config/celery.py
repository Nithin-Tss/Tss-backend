"""
Celery app: background jobs and the schedule that replaces the housekeeping cron entries.

    celery -A config worker -l info
    celery -A config beat -l info          # exactly one beat process

Needs CELERY_BROKER_URL (or REDIS_URL). Without either, tasks run inline in the calling
process, so local development and the test suite work with no broker.
"""
import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("tss")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

app.conf.beat_schedule = {
    "deliver-webhooks": {"task": "apps.core.tasks.deliver_webhooks", "schedule": 5.0},
    "sweep-webhooks": {"task": "apps.webhooks.tasks.sweep_webhooks", "schedule": 10.0},
    "expire-stock-reservations": {"task": "apps.core.tasks.expire_reservations", "schedule": 120.0},
    "sweep-carts": {"task": "apps.core.tasks.sweep_carts", "schedule": crontab(minute=7)},
    "prune-webhook-deliveries": {"task": "apps.core.tasks.prune_webhook_deliveries", "schedule": crontab(hour=3, minute=50)},
    "prune-audit-logs": {"task": "apps.core.tasks.prune_audit_logs", "schedule": crontab(hour=3, minute=30)},
    "aggregate-daily-metrics": {"task": "apps.core.tasks.aggregate_daily_metrics", "schedule": crontab(hour=1, minute=15)},
}
