"""
Support tickets, staff side: every ticket of every store. Platform administrators and support agents only
(IsSupportAgent: User.is_owner or User.is_support_agent), checked here on the backend whatever the frontend shows.

    GET   platform/support/tickets/                      all tickets
            ?status= (one or several, comma separated; "active" = open, in_progress, waiting_on_customer)
            ?priority= ?category= ?source= ?assigned=me|unassigned|<user id> ?store=<id> ?q= ?limit= ?offset=
            default order: most urgent first, then newest activity
    GET   platform/support/summary/                      {"openUnassigned": n}  for the navigation badge
    GET   platform/support/agents/                       who a ticket can be assigned to
    GET   platform/support/tickets/<id>/                 one ticket: summary, AI transcript, messages, internal notes
    PATCH platform/support/tickets/<id>/                 {status?, priority?, category?, assignedTo? (user id | null)}
    POST  platform/support/tickets/<id>/messages/        {message, attachments?, status?}  reply the merchant sees
                                                         {message, internal: true}         internal note, staff only
    POST  platform/support/tickets/<id>/attachments/     multipart, field "image": a picture for a reply

Replies, notes and changes are written to the audit log (never the message text).
"""
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Case, IntegerField, Q, Value, When
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.platform_admin.permissions import IsSupportAgent
from apps.platform_admin.platform_views import PlatformView, note, paged

from . import services
from .models import Ticket

User = get_user_model()

PRIORITY_RANK = Case(
    When(priority=Ticket.URGENT, then=Value(0)), When(priority=Ticket.HIGH, then=Value(1)),
    When(priority=Ticket.NORMAL, then=Value(2)), default=Value(3), output_field=IntegerField(),
)


class SupportView(PlatformView):
    permission_classes = [IsAuthenticated, IsSupportAgent]


def get_ticket(ticket_id):
    return get_object_or_404(Ticket.objects.select_related("store", "user", "assigned_to", "conversation"), pk=ticket_id)


def body_of(request):
    if not isinstance(request.data, dict):
        raise ValidationError({"detail": "Send a JSON object."})
    return request.data


def _csv(value, allowed):
    wanted = [v for v in (value or "").split(",") if v in allowed]
    return wanted


def _filtered(request, tickets):
    params = request.query_params
    raw_status = params.get("status") or ""
    if raw_status == "active":
        tickets = tickets.filter(status__in=Ticket.ACTIVE_STATUSES)
    elif statuses := _csv(raw_status, services.ALL_STATUSES):
        tickets = tickets.filter(status__in=statuses)
    if priorities := _csv(params.get("priority"), services.ALL_PRIORITIES):
        tickets = tickets.filter(priority__in=priorities)
    if categories := _csv(params.get("category"), services.ALL_CATEGORIES):
        tickets = tickets.filter(category__in=categories)
    if sources := _csv(params.get("source"), [k for k, _ in Ticket.SOURCES]):
        tickets = tickets.filter(source__in=sources)
    assigned = params.get("assigned")
    if assigned == "me":
        tickets = tickets.filter(assigned_to=request.user)
    elif assigned == "unassigned":
        tickets = tickets.filter(assigned_to__isnull=True)
    elif assigned:
        try:
            tickets = tickets.filter(assigned_to_id=assigned)
        except (ValueError, TypeError, DjangoValidationError):  # not a uuid: nothing matches
            tickets = tickets.none()
    if store := params.get("store"):
        tickets = tickets.filter(store_id=store)
    if term := (params.get("q") or "").strip():
        tickets = tickets.filter(
            Q(ticket_number__icontains=term) | Q(subject__icontains=term) | Q(store__store_name__icontains=term)
            | Q(user__email__icontains=term)
        )
    return tickets


class StaffTicketListView(SupportView):
    def get(self, request):
        tickets = services.with_list_fields(
            Ticket.objects.select_related("store", "user", "assigned_to"), staff=True,
        )
        tickets = _filtered(request, tickets).annotate(priority_rank=PRIORITY_RANK).order_by(
            "priority_rank", "-updated_at", "-created_at",
        )
        return paged(request, tickets, lambda t: services.ticket_json(t, staff=True))


class StaffSummaryView(SupportView):
    def get(self, request):
        count = Ticket.objects.filter(status=Ticket.OPEN, assigned_to__isnull=True).count()
        return Response({"openUnassigned": count})


class StaffAgentsView(SupportView):
    def get(self, request):
        agents = User.objects.filter(Q(is_owner=True) | Q(is_support_agent=True)).order_by("first_name", "email")
        return Response([
            {"id": str(u.pk), "name": u.get_full_name() or u.email, "email": u.email} for u in agents
        ])


class StaffTicketDetailView(SupportView):
    def get(self, request, ticket_id):
        return Response(services.ticket_detail_json(get_ticket(ticket_id), staff=True))

    def patch(self, request, ticket_id):
        ticket = get_ticket(ticket_id)
        data = body_of(request)
        if not {"status", "priority", "category", "assignedTo"} & data.keys():
            raise ValidationError({"detail": "Send status, priority, category or assignedTo."})
        new_status = services.clean_status(data["status"]) if "status" in data else None
        priority = services.clean_choice("priority", data["priority"], services.ALL_PRIORITIES) if "priority" in data else None
        category = services.clean_choice("category", data["category"], services.ALL_CATEGORIES) if "category" in data else None
        assignee, unassign = None, False
        if "assignedTo" in data:
            if data["assignedTo"] in (None, ""):
                unassign = True
            else:
                assignee = User.objects.filter(
                    Q(is_owner=True) | Q(is_support_agent=True), pk=_uuid(data["assignedTo"]),
                ).first()
                if assignee is None:
                    raise ValidationError({"assignedTo": "That person isn't a support agent."})
        before = {"status": ticket.status, "priority": ticket.priority, "assigned_to": ticket.assigned_to_id}
        changed = services.staff_update(
            ticket, status=new_status, priority=priority, category=category, assigned_to=assignee, unassign=unassign,
        )
        if changed:
            note(
                request, "support_status" if "status" in changed else "support_update", store=ticket.store, object_id=ticket.id, object_repr=ticket.ticket_number,
                changed=changed, old_status=before["status"], new_status=ticket.status,
            )
        return Response(services.ticket_detail_json(get_ticket(ticket_id), staff=True))


def _uuid(value):
    import uuid

    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise ValidationError({"assignedTo": "Not a valid user id."}) from None


class StaffTicketReplyView(SupportView):
    def post(self, request, ticket_id):
        ticket = get_ticket(ticket_id)
        data = body_of(request)
        if data.get("internal") is True:
            fields = services.clean_reply({"message": data.get("message")})
            services.staff_note(ticket, user=request.user, body=fields["message"])
            note(request, "support_note", store=ticket.store, object_id=ticket.id, object_repr=ticket.ticket_number)
            return Response(services.ticket_detail_json(get_ticket(ticket_id), staff=True), status=status.HTTP_201_CREATED)
        fields = services.clean_reply(data)
        new_status = services.clean_status(data.get("status", Ticket.WAITING))
        attachments = services.resolve_attachments(
            request, fields["attachments"], store_id=ticket.store_id, user=request.user,
        )
        services.staff_reply(ticket, user=request.user, fields=fields, attachments=attachments, status=new_status)
        note(
            request, "support_reply", store=ticket.store, object_id=ticket.id, object_repr=ticket.ticket_number,
            new_status=new_status,
        )
        return Response(services.ticket_detail_json(get_ticket(ticket_id), staff=True), status=status.HTTP_201_CREATED)


class StaffTicketAttachmentView(SupportView):
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, ticket_id):
        ticket = get_ticket(ticket_id)
        asset = services.upload_attachment(request, ticket.store_id)
        return Response(services.attachment_json(request, asset), status=status.HTTP_201_CREATED)
