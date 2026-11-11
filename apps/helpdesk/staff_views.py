"""
Help Center tickets, staff side: every ticket of every store. Platform administrators only (User.is_owner), the same
PlatformView as the rest of /api/v1/platform/ (see apps/platform_admin/platform_views.py).

    GET   platform/support/tickets/                      all tickets (?status=, ?store=<id>, ?q=, ?limit=, ?offset=)
    GET   platform/support/tickets/<id>/                 one ticket with its messages
    PATCH platform/support/tickets/<id>/                 {status}
    POST  platform/support/tickets/<id>/messages/        {message, attachments?, status?}   default status: pending
    POST  platform/support/tickets/<id>/attachments/     multipart, field "image": a picture for a reply

Replies and status changes are written to the audit log (never the message text).
"""
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from apps.platform_admin.platform_views import PlatformView, note, paged

from . import services
from .models import Ticket, TicketMessage


def get_ticket(ticket_id):
    return get_object_or_404(Ticket.objects.select_related("store", "user"), pk=ticket_id)


def body_of(request):
    if not isinstance(request.data, dict):
        raise ValidationError({"detail": "Send a JSON object."})
    return request.data


class StaffTicketListView(PlatformView):
    def get(self, request):
        tickets = services.with_list_fields(Ticket.objects.select_related("store", "user"))
        params = request.query_params
        if params.get("status") in (Ticket.OPEN, Ticket.PENDING, Ticket.CLOSED):
            tickets = tickets.filter(status=params["status"])
        if store := params.get("store"):
            tickets = tickets.filter(store_id=store)
        if term := (params.get("q") or "").strip():
            tickets = tickets.filter(
                Q(subject__icontains=term) | Q(store__store_name__icontains=term) | Q(user__email__icontains=term)
            )
        return paged(request, tickets, lambda t: services.ticket_json(t, staff=True))


class StaffTicketDetailView(PlatformView):
    def get(self, request, ticket_id):
        return Response(services.ticket_detail_json(get_ticket(ticket_id), staff=True))

    def patch(self, request, ticket_id):
        ticket = get_ticket(ticket_id)
        new_status = services.clean_status(body_of(request).get("status"))
        if new_status != ticket.status:
            before = ticket.status
            services.touch(ticket, new_status)
            note(
                request, "support_status", store=ticket.store, object_id=ticket.id, object_repr=ticket.subject,
                old_status=before, new_status=new_status,
            )
        return Response(services.ticket_detail_json(ticket, staff=True))


class StaffTicketReplyView(PlatformView):
    def post(self, request, ticket_id):
        ticket = get_ticket(ticket_id)
        data = body_of(request)
        fields = services.clean_reply(data)
        new_status = services.clean_status(data.get("status", Ticket.PENDING))
        attachments = services.resolve_attachments(
            request, fields["attachments"], store_id=ticket.store_id, user=request.user,
        )
        services.add_message(
            ticket, sender_type=TicketMessage.STAFF, user=request.user, body=fields["message"],
            attachments=attachments, status=new_status,
        )
        note(
            request, "support_reply", store=ticket.store, object_id=ticket.id, object_repr=ticket.subject,
            new_status=new_status,
        )
        return Response(services.ticket_detail_json(ticket, staff=True), status=status.HTTP_201_CREATED)


class StaffTicketAttachmentView(PlatformView):
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, ticket_id):
        ticket = get_ticket(ticket_id)
        asset = services.upload_attachment(request, ticket.store_id)
        return Response(services.attachment_json(request, asset), status=status.HTTP_201_CREATED)
