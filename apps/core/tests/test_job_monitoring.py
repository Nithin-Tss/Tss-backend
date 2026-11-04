"""Background-job monitoring: heartbeat, health endpoint, failure logging."""
import time
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.core import monitoring
from apps.identity.models import User
from apps.webhooks.models import FAILED, RECEIVED, IncomingWebhookReceipt

URL = "/api/v1/health/jobs/"


def receipt(status, age_minutes, event_id):
    row = IncomingWebhookReceipt.objects.create(
        provider="stripe", external_event_id=event_id, payload={}, status=status,
    )
    IncomingWebhookReceipt.objects.filter(pk=row.pk).update(received_at=timezone.now() - timedelta(minutes=age_minutes))


@override_settings(JOBS_HEALTH_TOKEN="pinger-token", CELERY_TASK_ALWAYS_EAGER=False)
class JobHealthTests(TestCase):
    def setUp(self):
        cache.clear()
        patch = mock.patch.object(monitoring, "queue_length", return_value=3)
        patch.start()
        self.addCleanup(patch.stop)

    def get(self, **headers):
        return self.client.get(URL, **headers)

    def test_hidden_without_token_or_admin(self):
        self.assertEqual(self.get().status_code, 404)
        self.assertEqual(self.get(HTTP_X_HEALTH_TOKEN="wrong").status_code, 404)

    def test_no_heartbeat_is_unhealthy(self):
        response = self.get(HTTP_X_HEALTH_TOKEN="pinger-token")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "unhealthy")

    def test_fresh_heartbeat_is_ok(self):
        monitoring.stamp_heartbeat()
        response = self.get(HTTP_X_HEALTH_TOKEN="pinger-token")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["queueLength"], 3)

    def test_stale_heartbeat_is_unhealthy(self):
        cache.set(monitoring.HEARTBEAT_KEY, time.time() - 600, None)
        self.assertEqual(self.get(HTTP_X_HEALTH_TOKEN="pinger-token").status_code, 503)

    def test_stuck_and_failed_stripe_events_are_reported(self):
        monitoring.stamp_heartbeat()
        receipt(RECEIVED, 10, "evt_old")
        receipt(RECEIVED, 0, "evt_new")  # still inside the grace period
        receipt(FAILED, 1, "evt_failed")
        body = self.get(HTTP_X_HEALTH_TOKEN="pinger-token").json()
        self.assertEqual((body["stuckStripeEvents"], body["failedStripeEvents"]), (1, 1))
        self.assertEqual(len(body["problems"]), 2)

    @override_settings(JOBS_QUEUE_ALERT_LENGTH=2)
    def test_queue_backlog_is_unhealthy(self):
        monitoring.stamp_heartbeat()
        self.assertEqual(self.get(HTTP_X_HEALTH_TOKEN="pinger-token").status_code, 503)

    def test_platform_admin_can_read_without_token(self):
        monitoring.stamp_heartbeat()
        admin = User.objects.create_user(email="owner@example.com", password="Owner-pass-123", first_name="O", last_name="W")
        User.objects.filter(pk=admin.pk).update(is_owner=True)
        self.client.force_login(admin)
        self.assertEqual(self.get().status_code, 200)

    @override_settings(JOBS_HEALTH_TOKEN="")
    def test_empty_token_never_matches(self):
        self.assertEqual(self.get(HTTP_X_HEALTH_TOKEN="").status_code, 404)


class HeartbeatTaskTests(TestCase):
    def test_heartbeat_task_stamps_cache(self):
        from apps.core.tasks import heartbeat

        cache.clear()
        heartbeat()
        self.assertIsNotNone(cache.get(monitoring.HEARTBEAT_KEY))

    def test_failure_signal_logs_error(self):
        with self.assertLogs("tss.jobs", level="ERROR") as logs:
            monitoring.log_task_failure(sender=mock.Mock(), task_id="abc", exception=ValueError("boom"), args=(1,), kwargs={}, einfo="trace")
        self.assertIn("boom", logs.output[0])
