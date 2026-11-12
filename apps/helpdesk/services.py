"""
Help Center tickets: the rules shared by the owner endpoints (views.py) and the staff endpoints (staff_views.py).

Status: open = waiting for staff, in_progress = staff is on it, waiting_on_customer = staff replied and waits for the
owner, resolved / closed = done.
An owner's reply reopens a finished ticket (open), answers a waiting one (in_progress when someone is assigned, else
open) and leaves an in_progress one alone.
A staff reply puts it to waiting_on_customer, unless the staff member picks another status.
Internal notes (is_internal) are staff-only: the owner endpoints never return them and nothing is emailed for them.
"""
import logging
import uuid

from django.conf import settings
from django.db import transaction
from django.db.models import Count, OuterRef, Q, Subquery
from django.utils import timezone
from rest_framework import exceptions

# Same limits and text cleaning as the older email-only form (apps/core/support.py)
from apps.core.support import MAX_MESSAGE, MAX_PAGE, MAX_SUBJECT, MAX_TRANSCRIPT, MIN_MESSAGE, _text
from apps.core.tasks import send_store_email
from apps.media.images import public_image_url
from apps.media.models import MediaAsset
from apps.media.services import upload_image

from . import live
from .models import MAX_BODY, Ticket, TicketMessage

logger = logging.getLogger(__name__)

MAX_ATTACHMENTS = 3
MAX_MESSAGES_SHOWN = 500
STAFF_NAME = "Support team"  # what an owner sees for any staff reply: never the staff member's own name


# ---------------------------------------------------------------- input

def _object(data):
    if not isinstance(data, dict):
        raise exceptions.ValidationError({"detail": "Send a JSON object."})
    return data


def clean_attachment_ids(data):
    """The ids of the pictures to attach (from the attachments endpoint): at most 3, no repeats."""
    raw = data.get("attachments")
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise exceptions.ValidationError({"attachments": "Must be a list."})
    if len(raw) > MAX_ATTACHMENTS:
        raise exceptions.ValidationError({"attachments": f"Attach at most {MAX_ATTACHMENTS} images."})
    ids = []
    for item in raw:
        try:
            value = uuid.UUID(str(item))
        except ValueError:
            raise exceptions.ValidationError({"attachments": "Not a valid image."}) from None
        if value not in ids:
            ids.append(value)
    return ids


def clean_new_ticket(data):
    data = _object(data)
    return {
        "subject": _text(data, "subject", required=True, max_length=MAX_SUBJECT, single_line=True),
        "message": _text(data, "message", required=True, max_length=MAX_MESSAGE, min_length=MIN_MESSAGE),
        "transcript": _text(data, "transcript", required=False, max_length=MAX_TRANSCRIPT),
        "page": _text(data, "page", required=False, max_length=MAX_PAGE, single_line=True),
        "include_details": data.get("includeStoreDetails") is True,
        "attachments": clean_attachment_ids(data),
    }


def clean_reply(data):
    data = _object(data)
    return {
        "message": _text(data, "message", required=True, max_length=MAX_MESSAGE),
        "attachments": clean_attachment_ids(data),
    }


ALL_STATUSES = tuple(key for key, _ in Ticket.STATUSES)
ALL_PRIORITIES = tuple(key for key, _ in Ticket.PRIORITIES)
ALL_CATEGORIES = tuple(key for key, _ in Ticket.CATEGORIES)


def clean_choice(field, value, allowed):
    if value not in allowed:
        raise exceptions.ValidationError({field: f"Must be one of: {', '.join(allowed)}."})
    return value


def clean_status(value, allowed=ALL_STATUSES):
    return clean_choice("status", value, allowed)


# ---------------------------------------------------------------- pictures

def upload_attachment(request, store_id):
    """Keep one screenshot with the existing media upload rules (type, 1MB, EXIF removed, quota). -> MediaAsset"""
    return upload_image(store_id, request.FILES.get("image"), request.user)


def attachment_json(request, asset):
    return {"id": str(asset.id), "name": asset.original_name, "url": public_image_url(request, asset.url)}


def resolve_attachments(request, ids, *, store_id, user):
    """
    The addresses to save for these picture ids. A picture counts only if it belongs to the ticket's store and the
    sender uploaded it, so nobody can attach another store's picture or an arbitrary address.
    """
    if not ids:
        return []
    assets = {a.id: a for a in MediaAsset.objects.filter(store_id=store_id, uploaded_by=user, id__in=ids).defer("data")}
    if len(assets) != len(ids):
        raise exceptions.ValidationError({"attachments": "One of the images could not be found. Upload it again."})
    return [public_image_url(request, assets[i].url) for i in ids]


# ---------------------------------------------------------------- writing

def build_first_body(fields, store, user):
    """The problem, then (only when the owner agreed) the store details and the assistant chat."""
    parts = [fields["message"]]
    if fields["include_details"]:
        parts.append("\n".join([
            "--- Details ---",
            f"Store: {' '.join(str(store.store_name).split())}",
            f"Store ID: {store.store_id}",
            f"Name: {user.get_full_name() or '-'}",
            f"Email: {user.email}",
            f"Page I came from: {fields['page'] or '-'}",
        ]))
    if fields["transcript"]:
        parts.append(f"--- Chat with the assistant ---\n{fields['transcript']}")
    return "\n\n".join(parts)[:MAX_BODY]


def touch(ticket, status=None, *, extra_fields=()):
    """Save the ticket with its new status. resolved_at is set when it becomes resolved/closed and cleared on reopen."""
    fields = ["status", "updated_at", *extra_fields]
    if status:
        ticket.status = status
    if ticket.status in Ticket.FINISHED_STATUSES:
        if ticket.resolved_at is None:
            ticket.resolved_at = timezone.now()
            fields.append("resolved_at")
    elif ticket.resolved_at is not None:
        ticket.resolved_at = None
        fields.append("resolved_at")
    ticket.save(update_fields=fields)  # updated_at is auto_now: listing it moves it forward
    live.publish(ticket)  # every new message and every change goes through here


def create_ticket(*, store, user, fields, attachments):
    """A ticket from the Help Center form."""
    with transaction.atomic():
        ticket = Ticket.objects.create(store=store, user=user, subject=fields["subject"])
        TicketMessage.objects.create(
            ticket=ticket, sender_type=TicketMessage.OWNER, sender=user,
            body=build_first_body(fields, store, user), attachments=attachments,
        )
    notify_support(
        f"[Support] {_store_name(store)}: {ticket.subject}", fields["message"], ticket=ticket, user=user,
    )
    return ticket


def owner_status_after_reply(ticket):
    if ticket.status == Ticket.IN_PROGRESS:
        return Ticket.IN_PROGRESS
    if ticket.status == Ticket.WAITING and ticket.assigned_to_id:
        return Ticket.IN_PROGRESS
    return Ticket.OPEN


def add_message(ticket, *, sender_type, user, body, attachments, status=None, is_internal=False):
    """
    Add one message and move the ticket's status. Owner -> see owner_status_after_reply. Staff -> `status` (default
    waiting_on_customer). An internal note never changes the status (it still counts as activity on the ticket).
    """
    if is_internal:
        status = ticket.status
    elif status is None:
        status = owner_status_after_reply(ticket) if sender_type == TicketMessage.OWNER else Ticket.WAITING
    with transaction.atomic():
        message = TicketMessage.objects.create(
            ticket=ticket, sender_type=sender_type, sender=user, body=body, attachments=attachments,
            is_internal=is_internal,
        )
        touch(ticket, status)
    return message


def owner_reply(ticket, *, user, fields, attachments):
    message = add_message(
        ticket, sender_type=TicketMessage.OWNER, user=user, body=fields["message"], attachments=attachments,
    )
    notify_support(
        f"[Support] {_store_name(ticket.store)}: new reply on \"{ticket.subject}\"",
        fields["message"], ticket=ticket, user=user,
    )
    return message


def _store_name(store):
    return " ".join(str(store.store_name).split())


def notify_support(subject, text, *, ticket, user):
    """Email SUPPORT_EMAIL about a ticket, only when that setting is set. A failed send never fails the request."""
    if not settings.SUPPORT_EMAIL:
        return
    body = (
        f"{text}\n\n--- Ticket ---\nTicket ID: {ticket.id}\nStore: {_store_name(ticket.store)}\n"
        f"From: {user.get_full_name() or '-'} <{user.email}>"
    )
    try:
        send_store_email(subject, body, [settings.SUPPORT_EMAIL], reply_to=user.email)
    except Exception:
        logger.exception("Support email could not be sent (ticket %s)", ticket.id)


def notify_merchant(ticket, subject, text):
    """Email the member who opened the ticket. A failed send never fails the request."""
    user = ticket.user
    if user is None or not user.email:
        return
    body = f"{text}\n\nTicket {ticket.ticket_number}: {ticket.subject}\nOpen it in your admin under Help > My support tickets."
    try:
        send_store_email(subject, body, [user.email])
    except Exception:
        logger.exception("Merchant email could not be sent (ticket %s)", ticket.id)


def staff_reply(ticket, *, user, fields, attachments, status):
    """A reply the merchant sees. Emails them the reply."""
    message = add_message(
        ticket, sender_type=TicketMessage.STAFF, user=user, body=fields["message"], attachments=attachments,
        status=status,
    )
    notify_merchant(
        ticket, f"[{ticket.ticket_number}] New reply from our support team", fields["message"],
    )
    return message


def staff_note(ticket, *, user, body):
    """An internal note: staff only, no email, status unchanged."""
    return add_message(
        ticket, sender_type=TicketMessage.STAFF, user=user, body=body, attachments=[], is_internal=True,
    )


def staff_update(ticket, *, status=None, priority=None, category=None, assigned_to=None, unassign=False):
    """
    Apply the controls of the staff detail page. Returns the names of the fields that changed. Assigning a
    still-open ticket starts work on it (in_progress). A status change emails the merchant.
    """
    changed = []
    extra = []
    if priority is not None and priority != ticket.priority:
        ticket.priority = priority
        extra.append("priority")
        changed.append("priority")
    if category is not None and category != ticket.category:
        ticket.category = category
        extra.append("category")
        changed.append("category")
    if unassign and ticket.assigned_to_id:
        ticket.assigned_to = None
        extra.append("assigned_to")
        changed.append("assigned_to")
    elif assigned_to is not None and assigned_to.pk != ticket.assigned_to_id:
        ticket.assigned_to = assigned_to
        extra.append("assigned_to")
        changed.append("assigned_to")
        if status is None and ticket.status == Ticket.OPEN:
            status = Ticket.IN_PROGRESS
    before = ticket.status
    new_status = status if status is not None and status != before else None
    if new_status or extra:
        touch(ticket, new_status, extra_fields=extra)
    if new_status:
        changed.append("status")
        notify_merchant(
            ticket, f"[{ticket.ticket_number}] Your support ticket is now {dict(Ticket.STATUSES)[new_status].lower()}",
            f"The status of your support ticket changed from {dict(Ticket.STATUSES)[before].lower()} "
            f"to {dict(Ticket.STATUSES)[new_status].lower()}.",
        )
    return changed


# ---------------------------------------------------------------- reading

def with_list_fields(queryset, *, staff=False):
    """Adds message_count, last_sender and last_message_at to each ticket of the list (internal notes: staff only)."""
    messages = TicketMessage.objects.filter(ticket=OuterRef("pk"))
    count_filter = None
    if not staff:
        messages = messages.filter(is_internal=False)
        count_filter = Q(messages__is_internal=False)
    last = messages.order_by("-created_at", "-id")
    return queryset.annotate(
        message_count=Count("messages", filter=count_filter),
        last_sender=Subquery(last.values("sender_type")[:1]),
        last_message_at=Subquery(last.values("created_at")[:1]),
    )


def ticket_json(ticket, *, staff=False):
    data = {
        "id": str(ticket.id),
        "ticketNumber": ticket.ticket_number,
        "subject": ticket.subject,
        "status": ticket.status,
        "priority": ticket.priority,
        "category": ticket.category,
        "source": ticket.source,
        "createdAt": ticket.created_at,
        "updatedAt": ticket.updated_at,
        "resolvedAt": ticket.resolved_at,
        "messageCount": getattr(ticket, "message_count", None),
        "lastSender": getattr(ticket, "last_sender", None),
        "lastMessageAt": getattr(ticket, "last_message_at", None),
    }
    if staff:
        user = ticket.user
        assignee = ticket.assigned_to
        data["store"] = {"id": str(ticket.store_id), "name": ticket.store.store_name}
        data["user"] = {"email": user.email, "name": user.get_full_name()} if user else None
        data["assignedTo"] = (
            {"id": str(assignee.pk), "name": assignee.get_full_name() or assignee.email, "email": assignee.email}
            if assignee else None
        )
    return data


def message_json(message, *, staff=False):
    if message.sender_type == TicketMessage.STAFF and not staff:
        name = STAFF_NAME
    else:
        sender = message.sender
        name = (sender.get_full_name() or sender.email) if sender else "Former user"
    data = {
        "id": str(message.id),
        "senderType": message.sender_type,
        "senderName": name,
        "body": message.body,
        "attachments": message.attachments or [],
        "createdAt": message.created_at,
    }
    if staff:
        data["isInternal"] = message.is_internal
    return data


def transcript_json(ticket):
    """The help-assistant chat behind the ticket (both sides see it: the merchant wrote it), oldest first."""
    conversation = ticket.conversation
    if conversation is None:
        return []
    rows = conversation.messages.order_by("created_at")[:MAX_MESSAGES_SHOWN]
    return [{"role": m.role, "content": m.content, "createdAt": m.created_at} for m in rows]


def ticket_detail_json(ticket, *, staff=False):
    messages = ticket.messages.select_related("sender")
    if not staff:
        messages = messages.filter(is_internal=False)  # internal notes never reach the merchant
    return {
        **ticket_json(ticket, staff=staff),
        "summary": ticket.summary,
        "transcript": transcript_json(ticket),
        "messages": [message_json(m, staff=staff) for m in messages[:MAX_MESSAGES_SHOWN]],
    }
