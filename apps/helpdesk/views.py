"""
Help Center tickets, owner side (a store member). Mounted at /api/v1/ (see urls.py):

    POST support/tickets/                      {subject, message, transcript?, includeStoreDetails?, page?, attachments?}
    GET  support/tickets/                      my tickets (?status=, ?limit=, ?offset=)
    GET  support/tickets/<id>/                 one ticket with its messages
    POST support/tickets/<id>/messages/        {message, attachments?}   a reply (reopens the ticket)
    POST support/attachments/                  multipart, field "image": one screenshot -> {id, name, url}

Same permissions as apps/core/support.py: any active member of the store, no API key. A member sees only the tickets
they opened in the store of the request; anyone else's ticket is a 404. Like that endpoint, these requests are taken
out of the audit log (a ticket can hold the assistant chat).
"""
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from apps.core.permissions import IsStoreMember
from apps.core.support import NoApiKeys
from apps.platform_admin.platform_views import paged

from . import services
from .models import Ticket


class UserThrottle(SimpleRateThrottle):
    """One allowance per signed-in user (rates: DEFAULT_THROTTLE_RATES[scope])."""

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": request.user.pk}


class NewTicketThrottle(UserThrottle):
    scope = "support_ticket"


class ReplyThrottle(UserThrottle):
    scope = "support_reply"


class AttachThrottle(UserThrottle):
    scope = "support_attach"


class OwnerView(APIView):
    permission_classes = [IsAuthenticated, NoApiKeys, IsStoreMember]
    write_throttle = None  # throttle class applied to POST, on top of the default one

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        # IsStoreMember registered this request for auditing; a support request is not audited (see module doc)
        request._request.audit = None

    def get_throttles(self):
        throttles = super().get_throttles()
        if self.write_throttle and self.request.method == "POST":
            throttles.append(self.write_throttle())
        return throttles

    def my_tickets(self):
        return Ticket.objects.filter(store=self.request.store, user=self.request.user)

    def my_ticket(self, ticket_id):
        return get_object_or_404(self.my_tickets().select_related("store"), pk=ticket_id)


class TicketListCreateView(OwnerView):
    write_throttle = NewTicketThrottle

    def get(self, request):
        tickets = services.with_list_fields(self.my_tickets())
        wanted = request.query_params.get("status")
        if wanted in (Ticket.OPEN, Ticket.PENDING, Ticket.CLOSED):
            tickets = tickets.filter(status=wanted)
        return paged(request, tickets, services.ticket_json)

    def post(self, request):
        fields = services.clean_new_ticket(request.data)
        attachments = services.resolve_attachments(
            request, fields["attachments"], store_id=request.store.store_id, user=request.user,
        )
        ticket = services.create_ticket(
            store=request.store, user=request.user, fields=fields, attachments=attachments,
        )
        return Response(services.ticket_detail_json(ticket), status=status.HTTP_201_CREATED)


class TicketDetailView(OwnerView):
    def get(self, request, ticket_id):
        return Response(services.ticket_detail_json(self.my_ticket(ticket_id)))


class TicketReplyView(OwnerView):
    write_throttle = ReplyThrottle

    def post(self, request, ticket_id):
        ticket = self.my_ticket(ticket_id)
        fields = services.clean_reply(request.data)
        attachments = services.resolve_attachments(
            request, fields["attachments"], store_id=request.store.store_id, user=request.user,
        )
        services.owner_reply(ticket, user=request.user, fields=fields, attachments=attachments)
        return Response(services.ticket_detail_json(ticket), status=status.HTTP_201_CREATED)


class AttachmentView(OwnerView):
    """Not the media library endpoint: that one needs the themes permission, a screenshot for support must not."""

    write_throttle = AttachThrottle
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        asset = services.upload_attachment(request, request.store.store_id)
        return Response(services.attachment_json(request, asset), status=status.HTTP_201_CREATED)
