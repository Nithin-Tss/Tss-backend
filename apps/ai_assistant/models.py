import uuid

from django.conf import settings
from django.db import models

from apps.tenancy.models import Store
from apps.themes.models import StorePage


class Conversation(models.Model):
    """One chat of one staff member in one store. Never readable by another store or another person."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, db_column="store_id", related_name="ai_conversations")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, db_column="user_id", related_name="ai_conversations",
    )
    # Which assistant owns this chat. The page builder and the help assistant never see each other's chats.
    PAGE_BUILDER, HELP = "page_builder", "help"
    ASSISTANT_TYPES = [(PAGE_BUILDER, "Page builder"), (HELP, "Help assistant")]
    assistant_type = models.CharField(max_length=20, choices=ASSISTANT_TYPES, default=PAGE_BUILDER)
    # Help assistant: a support ticket was raised from this chat, so the AI stops answering in it
    escalated = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"ai_assistant"."conversations"'
        indexes = [models.Index(fields=["store", "user", "-updated_at"], name="ix_ai_conv_store_user")]


class Message(models.Model):
    """A visible turn: what the merchant typed, or the assistant's reply plus the changes it made."""

    USER = "user"
    ASSISTANT = "assistant"
    ROLES = [(USER, "User"), (ASSISTANT, "Assistant")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, db_column="conversation_id", related_name="messages",
    )
    role = models.CharField(max_length=10, choices=ROLES)
    content = models.TextField()
    # [{"action": "created_page", "page_id": ..., "handle": ..., "title": ..., "summary": ...}]
    changes = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = '"ai_assistant"."messages"'
        ordering = ["created_at"]
        indexes = [models.Index(fields=["conversation", "created_at"], name="ix_ai_msg_conv_created")]


class AiPageDraft(models.Model):
    """
    A page the assistant created or edited. Only these pages can be published or discarded through the
    assistant. `original` is what Discard goes back to when the page already existed.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, db_column="store_id", related_name="ai_page_drafts")
    page = models.OneToOneField(StorePage, on_delete=models.CASCADE, db_column="page_id", related_name="ai_draft")
    conversation = models.ForeignKey(
        Conversation, on_delete=models.SET_NULL, null=True, blank=True, db_column="conversation_id", related_name="drafts",
    )
    created_by_ai = models.BooleanField(default=False)
    # {"title": ..., "template": "page.x", "template_data": {order, sections} | None} before the assistant's first edit
    original = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = '"ai_assistant"."page_drafts"'
