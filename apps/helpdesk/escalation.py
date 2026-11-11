"""
Raising a support ticket from a help-assistant chat. ONE function creates the ticket, used by both ways in:

  - the model's raise_support_ticket tool (source "ai_escalation"), and
  - the "Talk to a human" button (source "customer_request"), which never depends on the model.

A conversation has at most one active ticket: raising again returns the existing one. Creating the ticket also marks
the conversation escalated, so the AI stops answering in it (the chat view checks that flag).
"""
import json
import logging
import re

from django.db import transaction

from apps.ai_assistant import llm
from apps.ai_assistant.models import Conversation

from . import services
from .models import Ticket

logger = logging.getLogger(__name__)

MAX_SUBJECT = 150
MAX_SUMMARY = 4000
TRANSCRIPT_FOR_SUMMARY = 20  # last messages sent to the model to write the summary
NO_MESSAGE_SUMMARY = "The merchant asked to talk to a person and has not written anything yet."


def clean_fields(subject, summary, category, priority):
    """Safe values for a ticket: text trimmed and cut, an unknown category or priority replaced by the default."""
    subject = " ".join(str(subject or "").split())[:MAX_SUBJECT] or "Help needed"
    summary = str(summary or "").replace("\r\n", "\n").strip()[:MAX_SUMMARY]
    return {
        "subject": subject,
        "summary": summary,
        "category": category if category in services.ALL_CATEGORIES else "other",
        "priority": priority if priority in services.ALL_PRIORITIES else Ticket.NORMAL,
    }


def active_ticket(conversation):
    return conversation.tickets.filter(status__in=Ticket.ACTIVE_STATUSES).order_by("-created_at").first()


@transaction.atomic
def _create_locked(store, user, conversation, source, values):
    # Lock the conversation: two simultaneous requests (or the tool and the button) cannot both create a ticket
    conversation = Conversation.objects.select_for_update().get(pk=conversation.pk)
    existing = active_ticket(conversation)
    if existing:
        return existing, False
    ticket = Ticket.objects.create(
        store=store, user=user, conversation=conversation, source=source,
        subject=values["subject"], summary=values["summary"], category=values["category"], priority=values["priority"],
    )
    conversation.escalated = True
    conversation.save(update_fields=["escalated", "updated_at"])
    return ticket, True


def create_or_get_ticket(*, store, user, conversation, source, subject, summary, category="other", priority="normal"):
    """-> (ticket, created). The caller has already checked that the conversation belongs to this store and user."""
    existing = active_ticket(conversation)
    if existing:
        return existing, False
    ticket, created = _create_locked(
        store, user, conversation, source, clean_fields(subject, summary, category, priority),
    )
    if created:
        services.notify_support(
            f"[Support] {services._store_name(store)}: {ticket.subject} ({ticket.ticket_number})",
            f"{ticket.summary}\n\nPriority: {ticket.priority}  Category: {ticket.category}  "
            f"Source: {ticket.get_source_display()}",
            ticket=ticket, user=user,
        )
    return ticket, created


# ---------------------------------------------------------------- "Talk to a human": summary with a safe fallback

SUMMARY_PROMPT = (
    "You write support tickets. Read the chat between a merchant and a help assistant and reply with ONLY a JSON "
    'object: {"subject": "<under 100 characters>", "summary": "<what the problem is and what was already tried, '
    'under 1200 characters>", "category": "payments|shipping|domains|themes|account|other", '
    '"priority": "low|normal|high|urgent"}. Use only what is in the chat. Do not invent details.'
)


def last_user_message(conversation):
    row = conversation.messages.filter(role="user").order_by("-created_at").first()
    return row.content if row else ""


def fallback_summary(conversation, note=""):
    """Used whenever the model cannot write the ticket: the merchant's own last words."""
    last = (note or last_user_message(conversation)).strip()
    if not last:
        return {"subject": "Customer asked for a human", "summary": NO_MESSAGE_SUMMARY, "category": "other", "priority": "normal"}
    return {
        "subject": f"Customer asked for a human: {' '.join(last.split())[:90]}",
        "summary": last, "category": "other", "priority": "normal",
    }


def _json_object(text):
    match = re.search(r"\{.*\}", text or "", re.S)
    data = json.loads(match.group(0)) if match else None
    if not isinstance(data, dict):
        raise ValueError("no JSON object")
    return data


def summarize(conversation, note=""):
    """
    {subject, summary, category, priority} for a ticket made from this chat. ONE model call; on ANY failure (no
    key, timeout, rubbish answer...) the fallback is returned instead. Never raises.
    """
    base = fallback_summary(conversation, note)
    rows = list(conversation.messages.order_by("-created_at")[:TRANSCRIPT_FOR_SUMMARY])[::-1]
    if not rows:
        return base  # nothing to summarise: the merchant's own note (or the plain request) is the ticket
    chat = "\n".join(f"{'Merchant' if m.role == 'user' else 'Assistant'}: {m.content}" for m in rows)
    if note:
        chat += f"\nMerchant (asking for a human): {note}"
    try:
        answer = llm.chat(
            [{"role": "system", "content": SUMMARY_PROMPT}, {"role": "user", "content": chat[:12000]}],
            model=llm.help_model_name(),
        )
        data = _json_object(getattr(answer, "content", "") or "")
        subject = str(data.get("subject") or "").strip()
        summary = str(data.get("summary") or "").strip()
        if not subject or not summary:
            raise ValueError("empty subject or summary")
        return {
            "subject": subject, "summary": summary,
            "category": data.get("category"), "priority": data.get("priority"),
        }
    except Exception:  # noqa: BLE001 - the ticket must be created whatever happened to the model call
        logger.warning("helpdesk: ticket summary fell back to the merchant's last message", exc_info=True)
        return base


def escalate_conversation(*, store, user, conversation, note=""):
    """The "Talk to a human" button. -> (ticket, created)."""
    existing = active_ticket(conversation)
    if existing:
        return existing, False
    values = summarize(conversation, note)
    return create_or_get_ticket(
        store=store, user=user, conversation=conversation, source=Ticket.CUSTOMER_REQUEST, **values,
    )
