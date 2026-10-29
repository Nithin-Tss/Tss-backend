import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone


class AuditLogQuerySet(models.QuerySet):
    """Audit rows are append-only: the only way to remove them is purge_older_than()."""

    def update(self, **kwargs):
        raise TypeError("Audit log entries can't be changed.")

    def delete(self):
        raise TypeError("Audit log entries can't be deleted; use purge_older_than().")

    def purge_older_than(self, days):
        """Retention: drop entries older than `days` days. Returns how many went."""
        cutoff = timezone.now() - timedelta(days=days)
        return self.filter(created_at__lt=cutoff)._raw_delete(self.db)


class AuditLog(models.Model):
    """
    One thing somebody did: a write to a store's data (with what changed), a
    request their role refused, or an account event such as signing in.
    Rows are never edited or deleted individually.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Null for account events that belong to no store (sign-in, password change)
    store = models.ForeignKey(
        "tenancy.Store", null=True, blank=True, on_delete=models.CASCADE,
        db_column="store_id", related_name="audit_logs",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        db_column="actor_id", related_name="+",
    )
    # Kept apart from the FK so the entry still names who it was after the account is gone
    actor_email = models.CharField(max_length=254, blank=True, default="")

    # "<area>.<verb>" such as "orders.create" or "products.update"; or "permission.denied", "auth.login"
    action = models.CharField(max_length=80)
    # The permission area (see apps/core/rbac.py), blank for account events
    resource = models.CharField(max_length=60, blank=True, default="")
    object_id = models.CharField(max_length=64, blank=True, default="")
    object_repr = models.CharField(max_length=200, blank=True, default="")

    method = models.CharField(max_length=10, blank=True, default="")
    path = models.CharField(max_length=300, blank=True, default="")
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True, default="")
    request_id = models.UUIDField(default=uuid.uuid4)

    # [{"model": "Product", "id": "...", "op": "update", "fields": {"title": {"from": "a", "to": "b"}}}]
    changes = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    objects = AuditLogQuerySet.as_manager()

    class Meta:
        db_table = '"audit"."audit_logs"'
        ordering = ["-created_at"]
        verbose_name = "Audit log entry"
        verbose_name_plural = "Audit log"
        indexes = [
            models.Index(fields=["store", "-created_at"], name="idx_audit_store_time"),
            models.Index(fields=["store", "action"], name="idx_audit_store_action"),
            models.Index(fields=["store", "resource", "object_id"], name="idx_audit_store_object"),
            models.Index(fields=["actor", "-created_at"], name="idx_audit_actor_time"),
        ]

    def __str__(self):
        return f"{self.action} by {self.actor_email or 'system'}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise TypeError("Audit log entries can't be changed.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("Audit log entries can't be deleted.")
