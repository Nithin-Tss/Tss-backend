"""
Help Center tickets: the rules shared by the owner endpoints (views.py) and the staff endpoints (staff_views.py).

Status: open = waiting for staff, pending = staff replied and waits for the owner, closed = done.
An owner's reply always puts the ticket back to open (that is how a closed ticket is reopened).
A staff reply puts it to pending, unless the staff member picks another status.
"""
import logging
import uuid

from django.conf import settings
from django.db import transaction
from django.db.models import Count, OuterRef, Subquery
from rest_framework import exceptions

# Same limits and text cleaning as the older email-only form (apps/core/support.py)
from apps.core.support import MAX_MESSAGE, MAX_PAGE, MAX_SUBJECT, MAX_TRANSCRIPT, MIN_MESSAGE, _text
from apps.core.tasks import send_store_email
from apps.media.images import public_image_url
from apps.media.models import MediaAsset
from apps.media.services import upload_image

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


def clean_status(value, allowed=(Ticket.OPEN, Ticket.PENDING, Ticket.CLOSED)):
    if value not in allowed:
        raise exceptions.ValidationError({"status": f"Must be one of: {', '.join(allowed)}."})
    return value


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


def touch(ticket, status=None):
    if status:
        ticket.status = status
    ticket.save(update_fields=["status", "updated_at"])  # updated_at is auto_now: listing it moves it forward


def create_ticket(*, store, user, fields, attachments):
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


def add_message(ticket, *, sender_type, user, body, attachments, status=None):
    """Add one message and move the ticket's status. Owner -> open. Staff -> `status` (default pending)."""
    if status is None:
        status = Ticket.OPEN if sender_type == TicketMessage.OWNER else Ticket.PENDING
    with transaction.atomic():
        message = TicketMessage.objects.create(
            ticket=ticket, sender_type=sender_type, sender=user, body=body, attachments=attachments,
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


# ---------------------------------------------------------------- reading

def with_list_fields(queryset):
    """Adds message_count, last_sender and last_message_at to each ticket of the list."""
    last = TicketMessage.objects.filter(ticket=OuterRef("pk")).order_by("-created_at", "-id")
    return queryset.annotate(
        message_count=Count("messages"),
        last_sender=Subquery(last.values("sender_type")[:1]),
        last_message_at=Subquery(last.values("created_at")[:1]),
    )


def ticket_json(ticket, *, staff=False):
    data = {
        "id": str(ticket.id),
        "subject": ticket.subject,
        "status": ticket.status,
        "createdAt": ticket.created_at,
        "updatedAt": ticket.updated_at,
        "messageCount": getattr(ticket, "message_count", None),
        "lastSender": getattr(ticket, "last_sender", None),
        "lastMessageAt": getattr(ticket, "last_message_at", None),
    }
    if staff:
        user = ticket.user
        data["store"] = {"id": str(ticket.store_id), "name": ticket.store.store_name}
        data["user"] = {"email": user.email, "name": user.get_full_name()} if user else None
    return data


def message_json(message, *, staff=False):
    if message.sender_type == TicketMessage.STAFF and not staff:
        name = STAFF_NAME
    else:
        sender = message.sender
        name = (sender.get_full_name() or sender.email) if sender else "Former user"
    return {
        "id": str(message.id),
        "senderType": message.sender_type,
        "senderName": name,
        "body": message.body,
        "attachments": message.attachments or [],
        "createdAt": message.created_at,
    }


def ticket_detail_json(ticket, *, staff=False):
    messages = ticket.messages.select_related("sender")[:MAX_MESSAGES_SHOWN]
    return {
        **ticket_json(ticket, staff=staff),
        "messages": [message_json(m, staff=staff) for m in messages],
    }
