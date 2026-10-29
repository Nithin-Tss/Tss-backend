import csv
import json

from django.http import HttpResponse
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import HasStorePermission, IsStoreMember

from . import services
from .models import AuditLog

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
EXPORT_LIMIT = 10000


_OPS = {"create": "Created", "update": "Updated", "delete": "Deleted"}
_LABELS = {  # what the screens call things
    "auth.signup": "Created an account", "auth.login": "Signed in", "auth.login_failed": "Failed sign-in attempt",
    "auth.logout": "Signed out", "auth.password_changed": "Changed password",
    "audit.export": "Downloaded the audit log", services.DENIED: "Was refused access",
}


def summary(entry):
    """One readable line: "Updated Product “Wool Shirt”", or what the request was when no row changed."""
    if entry.action in _LABELS:
        return _LABELS[entry.action]
    changes = entry.changes or []
    if changes:
        first = changes[0]
        # Prefer the record the request was about over rows made along the way
        main = next((c for c in changes if c.get("id") == entry.object_id), first)
        text = f"{_OPS.get(main['op'], main['op'].title())} {_humanize(main['model'])}"
        if main.get("repr"):
            text += f" “{main['repr']}”"
        extra = len(changes) - 1
        return text + (f" and {extra} other record{'s' if extra != 1 else ''}" if extra > 0 else "")
    return f"{entry.method} {entry.path}".strip()


def _humanize(model_name):
    import re

    return re.sub(r"(?<!^)(?=[A-Z])", " ", model_name).lower()


def entry_json(entry, detail=False):
    data = {
        "summary": summary(entry),
        "id": str(entry.id),
        "action": entry.action,
        "resource": entry.resource,
        "objectId": entry.object_id,
        "object": entry.object_repr,
        "actor": entry.actor_email or None,
        "actorId": str(entry.actor_id) if entry.actor_id else None,
        "method": entry.method,
        "path": entry.path,
        "statusCode": entry.status_code,
        "ipAddress": entry.ip_address,
        "createdAt": entry.created_at,
        "changedObjects": len(entry.changes or []),
    }
    if detail:
        data.update({
            "userAgent": entry.user_agent,
            "requestId": str(entry.request_id),
            "changes": entry.changes,
            "metadata": entry.metadata,
        })
    return data


def _int(request, name, default, maximum=None):
    raw = request.query_params.get(name)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValidationError({name: "Must be a whole number."})
    if value < 0:
        raise ValidationError({name: "Can't be negative."})
    return min(value, maximum) if maximum else value


def _moment(request, name, end_of_day=False):
    raw = request.query_params.get(name)
    if not raw:
        return None
    moment = parse_datetime(raw)
    if moment is None:
        day = parse_date(raw)
        if day is None:
            raise ValidationError({name: "Use an ISO date (2026-10-29) or date and time."})
        from datetime import datetime, time, timedelta

        from django.utils import timezone

        moment = timezone.make_aware(datetime.combine(day, time.min))
        if end_of_day:
            moment += timedelta(days=1)  # "to" a date includes that whole day
    return moment


def filtered(request, queryset):
    """
    ?action=orders.create   exact action ("orders.*" for everything in an area)
    ?resource=orders        permission area
    ?actor=ann@example.com  part of the actor's email
    ?objectId=<id>          everything done to one record
    ?method=DELETE
    ?outcome=denied         only requests a role refused
    ?q=text                 action, object or actor containing the text
    ?from=2026-10-01&to=2026-10-29
    """
    params = request.query_params

    if action := params.get("action"):
        queryset = queryset.filter(action__startswith=action[:-1]) if action.endswith("*") else queryset.filter(action=action)
    if resource := params.get("resource"):
        queryset = queryset.filter(resource=resource)
    if actor := params.get("actor"):
        queryset = queryset.filter(actor_email__icontains=actor)
    if object_id := params.get("objectId"):
        queryset = queryset.filter(object_id=object_id)
    if method := params.get("method"):
        queryset = queryset.filter(method=method.upper())
    if outcome := params.get("outcome"):
        if outcome != "denied":
            raise ValidationError({"outcome": "Only 'denied' is supported."})
        queryset = queryset.filter(action=services.DENIED)
    if q := params.get("q"):
        from django.db.models import Q

        queryset = queryset.filter(
            Q(action__icontains=q) | Q(object_repr__icontains=q) | Q(actor_email__icontains=q)
        )
    if start := _moment(request, "from"):
        queryset = queryset.filter(created_at__gte=start)
    if end := _moment(request, "to", end_of_day=True):
        queryset = queryset.filter(created_at__lt=end)
    return queryset


class AuditView(APIView):
    permission_classes = [IsAuthenticated, IsStoreMember, HasStorePermission]
    permission_resource = "audit"

    def entries(self):
        return AuditLog.objects.filter(store=self.request.store).select_related("actor")


class AuditLogListView(AuditView):
    """
    GET /api/v1/audit/logs/  who did what in this store, newest first (audit.view)

    Filters: action, resource, actor, objectId, method, outcome, q, from, to.
    Paged with limit (up to 200) and offset: {count, limit, offset, results}.
    """

    def get(self, request):
        queryset = filtered(request, self.entries())
        limit = _int(request, "limit", DEFAULT_LIMIT, MAX_LIMIT) or DEFAULT_LIMIT
        offset = _int(request, "offset", 0)
        return Response({
            "count": queryset.count(),
            "limit": limit,
            "offset": offset,
            "results": [entry_json(e) for e in queryset.order_by("-created_at", "-id")[offset:offset + limit]],
        })


class AuditLogDetailView(AuditView):
    """GET /api/v1/audit/logs/<id>/  one entry with every field that changed (audit.view)"""

    def get(self, request, entry_id):
        entry = self.entries().filter(pk=entry_id).first()
        if entry is None:
            raise NotFound("Audit log entry not found.")
        return Response(entry_json(entry, detail=True))


def _safe_cell(value):
    """Spreadsheets run cells that start with = + - @ as formulas; defuse them."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


class AuditLogExportView(AuditView):
    """GET /api/v1/audit/logs/export/  the filtered log as CSV, up to 10,000 rows (audit.export)"""

    action_permissions = {"GET": "audit.export"}

    def get(self, request):
        rows = filtered(request, self.entries()).order_by("-created_at", "-id")[:EXPORT_LIMIT]

        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="audit-log.csv"'
        writer = csv.writer(response)
        writer.writerow([
            "Time", "Actor", "Action", "Resource", "Object ID", "Object", "Method", "Path",
            "Status", "IP address", "Changes",
        ])
        count = 0
        for e in rows:
            count += 1
            writer.writerow([_safe_cell(v) for v in (
                e.created_at.isoformat(), e.actor_email, e.action, e.resource, e.object_id, e.object_repr,
                e.method, e.path, e.status_code, e.ip_address, json.dumps(e.changes, default=str),
            )])

        # Taking the log out of the system is itself something to account for
        services.record(
            "audit.export", request=request, store=request.store, user=request.user, resource="audit",
            status_code=200, metadata={"rows": count, "filters": dict(request.query_params.items())},
        )
        return response


class MyActivityView(APIView):
    """GET /api/v1/audit/my-activity/  your own sign-ins and account changes (any signed-in user)"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        entries = AuditLog.objects.filter(actor=request.user, action__startswith="auth.")[:50]
        return Response([entry_json(e) for e in entries])
