import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Length
from django.db.models.lookups import GreaterThanOrEqual, LessThanOrEqual

MAX_BODY = 8000  # the problem (2000) + the assistant chat (4000) + store details fit with room to spare


class Ticket(models.Model):
    """
    One Help Center conversation between a store member and the platform's support staff.
    open = waiting for staff, pending = staff replied and is waiting for the owner, closed = done.
    """

    OPEN, PENDING, CLOSED = "open", "pending", "closed"
    STATUSES = [(OPEN, "Open"), (PENDING, "Pending"), (CLOSED, "Closed")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(
        "tenancy.Store", on_delete=models.CASCADE, db_column="store_id", related_name="support_tickets",
    )
    # The member who opened it; kept (as NULL) if their account is deleted
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
        db_column="user_id", related_name="support_tickets",
    )
    subject = models.CharField(max_length=150)
    status = models.CharField(max_length=10, choices=STATUSES, default=OPEN)
    created_at = models.DateTimeField(auto_now_add=True)
    # Moved forward by every message and status change (the inbox sorts on it)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"helpdesk"."tickets"'
        ordering = ["-updated_at", "-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=["open", "pending", "closed"]), name="ck_ticket_status",
            ),
        ]
        indexes = [
            models.Index(fields=["store", "user", "-updated_at"], name="idx_ticket_store_user_upd"),
            models.Index(fields=["status", "-updated_at"], name="idx_ticket_status_upd"),
        ]

    def __str__(self):
        return f"{self.subject} ({self.status})"


class TicketMessage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ticket = models.ForeignKey(
        Ticket, on_delete=models.CASCADE, db_column="ticket_id", related_name="messages",
    )
    OWNER, STAFF = "owner", "staff"
    SENDER_TYPES = [(OWNER, "Owner"), (STAFF, "Staff")]
    sender_type = models.CharField(max_length=5, choices=SENDER_TYPES)
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
        db_column="sender_id", related_name="+",
    )
    body = models.TextField()
    # Up to 3 picture addresses (the limit is checked in services.py)
    attachments = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = '"helpdesk"."ticket_messages"'
        ordering = ["created_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(sender_type__in=["owner", "staff"]), name="ck_ticket_message_sender_type",
            ),
            models.CheckConstraint(
                condition=Q(GreaterThanOrEqual(Length("body"), 1), LessThanOrEqual(Length("body"), MAX_BODY)),
                name="ck_ticket_message_body_length",
            ),
        ]
        indexes = [models.Index(fields=["ticket", "created_at"], name="idx_ticket_message_ticket")]

    def __str__(self):
        return f"{self.sender_type}: {self.body[:40]}"
