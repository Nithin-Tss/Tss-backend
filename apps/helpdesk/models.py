import uuid

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector
from django.db import models, transaction
from django.db.models import Q
from django.db.models.functions import Length
from django.db.models.lookups import GreaterThanOrEqual, LessThanOrEqual

MAX_BODY = 8000  # the problem (2000) + the assistant chat (4000) + store details fit with room to spare


class PgGinIndex(GinIndex):
    """A GIN index on PostgreSQL. Elsewhere (the SQLite test database) a plain index on the same table, so tables still build."""

    def create_sql(self, model, schema_editor, using="", **kwargs):
        if schema_editor.connection.vendor == "postgresql":
            return super().create_sql(model, schema_editor, using=using, **kwargs)
        return models.Index(fields=["slug"], name=self.name).create_sql(model, schema_editor, using=using, **kwargs)


class TicketCounter(models.Model):
    """One row (pk=1) holding the last ticket number handed out. Locked while a number is taken, so numbers never repeat."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = '"helpdesk"."ticket_counter"'

    @classmethod
    def next_number(cls):
        """The next human-friendly number, e.g. "TSS-000123". Call inside a transaction."""
        cls.objects.get_or_create(pk=1)
        counter = cls.objects.select_for_update().get(pk=1)
        counter.last_number += 1
        counter.save(update_fields=["last_number"])
        return format_ticket_number(counter.last_number)


def format_ticket_number(number):
    return f"TSS-{number:06d}"


class Ticket(models.Model):
    """
    One support conversation between a store member and the platform's support staff, from the Help Center form,
    the help assistant's "raise_support_ticket" tool, or the "Talk to a human" button.
    open = waiting for staff, in_progress = a staff member is on it, waiting_on_customer = staff replied and wait for
    the merchant, resolved = fixed (the merchant can still reply to reopen it), closed = done.
    """

    OPEN, IN_PROGRESS, WAITING, RESOLVED, CLOSED = "open", "in_progress", "waiting_on_customer", "resolved", "closed"
    STATUSES = [
        (OPEN, "Open"), (IN_PROGRESS, "In progress"), (WAITING, "Waiting on customer"),
        (RESOLVED, "Resolved"), (CLOSED, "Closed"),
    ]
    ACTIVE_STATUSES = (OPEN, IN_PROGRESS, WAITING)  # a conversation with one of these already has a ticket
    FINISHED_STATUSES = (RESOLVED, CLOSED)

    LOW, NORMAL, HIGH, URGENT = "low", "normal", "high", "urgent"
    PRIORITIES = [(LOW, "Low"), (NORMAL, "Normal"), (HIGH, "High"), (URGENT, "Urgent")]

    CATEGORIES = [
        ("payments", "Payments"), ("shipping", "Shipping"), ("domains", "Domains"),
        ("themes", "Themes"), ("account", "Account"), ("other", "Other"),
    ]

    AI_ESCALATION, CUSTOMER_REQUEST, HELP_CENTER = "ai_escalation", "customer_request", "help_center"
    SOURCES = [
        (AI_ESCALATION, "AI escalation"), (CUSTOMER_REQUEST, "Customer request"), (HELP_CENTER, "Help Center form"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ticket_number = models.CharField(max_length=20, unique=True, editable=False)
    store = models.ForeignKey(
        "tenancy.Store", on_delete=models.CASCADE, db_column="store_id", related_name="support_tickets",
    )
    # The member who opened it (created_by); kept (as NULL) if their account is deleted
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
        db_column="user_id", related_name="support_tickets",
    )
    subject = models.CharField(max_length=150)
    # The AI-written (or fallback) summary of the problem and what was already tried; staff only see it as context
    summary = models.TextField(blank=True, default="")
    # The help-assistant chat this ticket came from, so staff see the full transcript
    conversation = models.ForeignKey(
        "ai_assistant.Conversation", null=True, blank=True, on_delete=models.SET_NULL,
        db_column="conversation_id", related_name="tickets",
    )
    source = models.CharField(max_length=20, choices=SOURCES, default=HELP_CENTER)
    status = models.CharField(max_length=20, choices=STATUSES, default=OPEN)
    priority = models.CharField(max_length=10, choices=PRIORITIES, default=NORMAL)
    category = models.CharField(max_length=20, choices=CATEGORIES, default="other")
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        db_column="assigned_to_id", related_name="assigned_tickets",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    # Moved forward by every message and status change (the inbox sorts on it)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = '"helpdesk"."tickets"'
        ordering = ["-updated_at", "-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=["open", "in_progress", "waiting_on_customer", "resolved", "closed"]),
                name="ck_ticket_status",
            ),
            models.CheckConstraint(
                condition=Q(priority__in=["low", "normal", "high", "urgent"]), name="ck_ticket_priority",
            ),
            models.CheckConstraint(
                condition=Q(source__in=["ai_escalation", "customer_request", "help_center"]), name="ck_ticket_source",
            ),
        ]
        indexes = [
            models.Index(fields=["store", "user", "-updated_at"], name="idx_ticket_store_user_upd"),
            models.Index(fields=["status", "-updated_at"], name="idx_ticket_status_upd"),
            models.Index(fields=["assigned_to", "status"], name="idx_ticket_assignee_status"),
            models.Index(fields=["conversation"], name="idx_ticket_conversation"),
        ]

    def save(self, *args, **kwargs):
        if not self.ticket_number:
            with transaction.atomic():
                self.ticket_number = TicketCounter.next_number()
                return super().save(*args, **kwargs)
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.ticket_number} {self.subject} ({self.status})"


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
    # An internal note: only support staff ever see it (never the merchant, never an email)
    is_internal = models.BooleanField(default=False)
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


class HelpArticle(models.Model):
    """A help-center article the help assistant answers from. Managed in the Django admin."""

    CATEGORIES = [
        ("store_settings", "Store settings"), ("payments", "Payments"), ("shipping", "Shipping"),
        ("discounts", "Discounts"), ("domains", "Domains"), ("themes", "Themes"), ("other", "Other"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=120, unique=True)
    category = models.CharField(max_length=30, choices=CATEGORIES, default="other")
    body = models.TextField(help_text="Markdown.")
    keywords = models.CharField(max_length=500, blank=True, default="", help_text="Comma-separated words merchants use.")
    is_published = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"helpdesk"."help_articles"'
        ordering = ["category", "title"]
        indexes = [
            models.Index(fields=["is_published", "category"], name="idx_help_article_pub_cat"),
            PgGinIndex(
                SearchVector("title", "keywords", "body", config="english"), name="gin_help_article_fts",
            ),
        ]

    def __str__(self):
        return self.title
