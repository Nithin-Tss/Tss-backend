import secrets
import uuid

from django.db import models

PENDING, SUCCEEDED, FAILED = "pending", "succeeded", "failed"


def new_secret():
    return "whsec_" + secrets.token_urlsafe(32)


class WebhookEndpoint(models.Model):
    """A URL a store wants told about events, and which events."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(
        "tenancy.Store", on_delete=models.CASCADE, db_column="store_id", related_name="webhook_endpoints",
    )
    url = models.URLField(max_length=500)
    description = models.CharField(max_length=200, blank=True, default="")
    # Event names such as "order.created"; "*" means every event
    events = models.JSONField(default=list)
    secret = models.CharField(max_length=80, default=new_secret)
    is_active = models.BooleanField(default=True)
    # Deliveries that failed for good in a row; the endpoint is switched off at the limit
    consecutive_failures = models.PositiveIntegerField(default=0)
    disabled_reason = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"webhooks"."webhook_endpoints"'

    def __str__(self):
        return self.url

    def wants(self, event_type):
        return "*" in self.events or event_type in self.events


class WebhookDelivery(models.Model):
    """One event queued for one endpoint, with the outcome of sending it."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    endpoint = models.ForeignKey(WebhookEndpoint, on_delete=models.CASCADE, db_column="endpoint_id", related_name="deliveries")
    event_id = models.UUIDField(default=uuid.uuid4)
    event_type = models.CharField(max_length=60)
    payload = models.JSONField()
    status = models.CharField(max_length=20, default=PENDING)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    last_status_code = models.PositiveIntegerField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True, default="")
    delivered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = '"webhooks"."webhook_deliveries"'
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "next_attempt_at"], name="webhook_due_idx")]
