import logging
import secrets
import uuid

from django.db import models
from django.db.models import Q
from django.utils import timezone

from . import crypto

logger = logging.getLogger(__name__)

PENDING, SUCCEEDED, FAILED = "pending", "succeeded", "failed"
# WebhookEvent (outbox) states
DISPATCHED = "dispatched"
# IncomingWebhookReceipt states
RECEIVED, PROCESSED = "received", "processed"


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
    # After a rotation the old secret keeps signing too until this time, so a receiver can switch over without a gap
    previous_secret = models.CharField(max_length=80, blank=True, default="")
    previous_secret_expires_at = models.DateTimeField(null=True, blank=True)
    # Authenticated-encrypted copies of the two secrets (see crypto.py). The plaintext columns above stay for now so
    # older code and a rollback keep working; once every row is encrypted they can be blanked in a later change.
    secret_encrypted = models.TextField(blank=True, default="")
    previous_secret_encrypted = models.TextField(blank=True, default="")
    secret_key_id = models.CharField(max_length=40, blank=True, default="")
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

    def _aad(self, field):
        return f"{self.pk}:{field}"

    def _read(self, plain, encrypted, field):
        """The decrypted value when there is one that verifies, else the plaintext column (legacy rows)."""
        if encrypted:
            try:
                return crypto.decrypt(encrypted, self._aad(field))
            except crypto.CryptoError as exc:
                logger.error("Webhook endpoint %s: could not decrypt %s (%s); using the stored plaintext", self.pk, field, exc)
        return plain

    def get_secret(self):
        return self._read(self.secret, self.secret_encrypted, "secret")

    def get_previous_secret(self):
        return self._read(self.previous_secret, self.previous_secret_encrypted, "previous_secret")

    def refresh_encrypted(self):
        """Re-encrypts both secrets from the plaintext columns. Returns the names of the fields it set."""
        if not crypto.is_enabled():
            return []
        self.secret_encrypted = crypto.encrypt(self.secret, self._aad("secret")) if self.secret else ""
        self.previous_secret_encrypted = (
            crypto.encrypt(self.previous_secret, self._aad("previous_secret")) if self.previous_secret else ""
        )
        self.secret_key_id = crypto.active_key_id()
        return ["secret_encrypted", "previous_secret_encrypted", "secret_key_id"]

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        if update_fields is None or {"secret", "previous_secret"} & set(update_fields):
            extra = self.refresh_encrypted()
            if update_fields is not None:
                kwargs["update_fields"] = list(set(update_fields) | set(extra))
        super().save(*args, **kwargs)

    def signing_secrets(self, now=None):
        """The current secret, then the previous one while its overlap lasts."""
        secrets_ = [self.get_secret()]
        previous = self.get_previous_secret()
        if previous and self.previous_secret_expires_at and self.previous_secret_expires_at > (now or timezone.now()):
            secrets_.append(previous)
        return secrets_

    def wants(self, event_type):
        return "*" in self.events or event_type in self.events


class WebhookEvent(models.Model):
    """
    The outbox: one business event, written in the same transaction as the change that caused it, so a rollback
    removes both and a crash after commit loses nothing. A dispatcher turns each pending event into WebhookDeliveries.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(
        "tenancy.Store", on_delete=models.CASCADE, db_column="store_id", related_name="webhook_events",
    )
    event_type = models.CharField(max_length=60)
    # One value per business operation, e.g. "order.created:<order id>"; (store, dedupe_key) is unique.
    dedupe_key = models.CharField(max_length=200)
    # Null until dispatch for events recorded before their lines exist; then built from resource_model/resource_id
    payload = models.JSONField(null=True, blank=True)
    resource_model = models.CharField(max_length=100, blank=True, default="")
    resource_id = models.CharField(max_length=64, blank=True, default="")
    status = models.CharField(max_length=20, default=PENDING)
    dispatch_attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=500, blank=True, default="")
    dispatched_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = '"webhooks"."webhook_events"'
        ordering = ["created_at"]
        indexes = [models.Index(fields=["status", "created_at"], name="webhook_event_due_idx")]
        constraints = [
            models.UniqueConstraint(fields=["store", "dedupe_key"], name="webhook_event_dedupe_uniq"),
            models.CheckConstraint(condition=~Q(dedupe_key=""), name="webhook_event_dedupe_not_empty"),
            models.CheckConstraint(
                condition=Q(status__in=[PENDING, DISPATCHED, FAILED]), name="webhook_event_status_valid",
            ),
        ]


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
    # The outbox event this came from (null for pings and rows created before the outbox existed)
    outbox_event = models.ForeignKey(
        WebhookEvent, null=True, blank=True, on_delete=models.SET_NULL, db_column="outbox_event_id", related_name="deliveries",
    )
    # The URL as it was when the delivery was created; blank on older rows, which fall back to the endpoint's URL
    destination_url = models.URLField(max_length=500, blank=True, default="")
    # Set while a worker owns the delivery. Every result is written only if lease_token still matches (fencing).
    lease_token = models.UUIDField(null=True, blank=True)
    locked_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = '"webhooks"."webhook_deliveries"'
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "next_attempt_at"], name="webhook_due_idx")]
        constraints = [
            models.UniqueConstraint(fields=["endpoint", "event_id"], name="webhook_delivery_event_uniq"),
            models.CheckConstraint(
                condition=(Q(lease_token__isnull=True, locked_until__isnull=True)
                           | Q(lease_token__isnull=False, locked_until__isnull=False)),
                name="webhook_delivery_lease_pair",
            ),
        ]


class IncomingWebhookReceipt(models.Model):
    """
    A verified webhook from a provider (Stripe), stored before the 2xx answer so it can be processed,
    retried and audited from PostgreSQL even if Redis or the worker is down.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.CharField(max_length=30)
    external_event_id = models.CharField(max_length=255)
    event_type = models.CharField(max_length=100, blank=True, default="")
    payload = models.JSONField()
    status = models.CharField(max_length=20, default=RECEIVED)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True, default="")
    lease_token = models.UUIDField(null=True, blank=True)
    locked_until = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = '"webhooks"."incoming_webhook_receipts"'
        ordering = ["received_at"]
        indexes = [models.Index(fields=["status", "next_attempt_at"], name="webhook_receipt_due_idx")]
        constraints = [
            models.UniqueConstraint(fields=["provider", "external_event_id"], name="webhook_receipt_event_uniq"),
            models.CheckConstraint(
                condition=Q(status__in=[RECEIVED, PROCESSED, FAILED]), name="webhook_receipt_status_valid",
            ),
            models.CheckConstraint(
                condition=(Q(lease_token__isnull=True, locked_until__isnull=True)
                           | Q(lease_token__isnull=False, locked_until__isnull=False)),
                name="webhook_receipt_lease_pair",
            ),
        ]
