"""
Audit logging.

A request that reaches a store passes IsStoreMember, which calls begin(); from
then on every model save made while handling it is noted (what changed, field
by field). AuditMiddleware turns the notes into one AuditLog row per request
once the response is known. Requests that a role refuses are logged too.

Account events that belong to no store (sign-in, password change) are logged
directly with record().
"""
import ipaddress
import json
import logging
import re
import uuid
from contextvars import ContextVar
from datetime import date, datetime, time
from decimal import Decimal

from django.conf import settings

from .models import AuditLog

logger = logging.getLogger(__name__)

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
MAX_CHANGES = 100  # objects noted per request; the rest are counted, not listed
MAX_VALUE_CHARS = 300

DENIED = "permission.denied"

# Bookkeeping columns: they change on every save and say nothing.
NOISE_FIELDS = frozenset({"created_at", "updated_at", "deleted_at"})
SENSITIVE = re.compile(r"pass(word|wd)?|secret|token|api[_-]?key|credential|private[_-]?key|signature", re.I)

_STANDARD_VERBS = {"create": "create", "update": "update", "partial_update": "update", "destroy": "delete"}
_METHOD_VERBS = {"POST": "post", "PUT": "put", "PATCH": "patch", "DELETE": "delete", "GET": "view"}

_current = ContextVar("audit_collector", default=None)


class Collector:
    """What one request has done so far."""

    def __init__(self, method):
        self.collect = method in WRITE_METHODS  # reads change nothing, so there is nothing to note
        self.store = None
        self.user = None
        self.area = ""
        self.verb = ""
        self.denied = None  # the permission key the role lacked
        self.changes = {}  # (model label, pk) -> change dict, in the order first seen
        self.deletes = []  # snapshots of rows about to be hard-deleted
        self.skipped = 0

    def note(self, key, change):
        if key in self.changes:
            merged = self.changes[key]
            # The same row saved twice in one request: keep the first "from" and the last "to"
            for name, diff in change["fields"].items():
                if merged["op"] == "create":
                    merged["fields"][name] = {"to": diff["to"]}
                elif name in merged["fields"]:
                    merged["fields"][name]["to"] = diff["to"]
                else:
                    merged["fields"][name] = diff
            if merged["op"] == "update":
                merged["fields"] = {k: v for k, v in merged["fields"].items() if v.get("from") != v.get("to")}
            return
        if len(self.changes) >= MAX_CHANGES:
            self.skipped += 1
            return
        self.changes[key] = change


def begin(request):
    """Start auditing this request. `request` is the DRF request; the notes live on the Django one."""
    collector = Collector(request.method)
    request._request.audit = collector
    _current.set(collector)
    return collector


def end():
    _current.set(None)


def current():
    return _current.get()


def bind(request, membership, area, view):
    """Remember who is acting and on what, once the view's area is known."""
    collector = getattr(request._request, "audit", None)
    if collector is None:
        return None
    collector.store = membership.store
    collector.user = request.user
    collector.area = area or ""
    action = getattr(view, "action", None)
    collector.verb = _STANDARD_VERBS.get(action) or action or _METHOD_VERBS.get(request.method, request.method.lower())
    return collector


def deny(request, permission):
    collector = getattr(request._request, "audit", None)
    if collector is not None:
        collector.denied = permission


# ---------------------------------------------------------------- snapshots

def auditable(model):
    from apps.core.rbac import area_for_model

    if model._meta.label in {"tenancy.AccessLog", "tenancy.Invitation"}:
        return False
    return model._meta.app_label == "tenancy" or area_for_model(model) is not None


def _plain(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "[binary]"
    if isinstance(value, (dict, list, tuple)):
        return json.loads(json.dumps(value, default=str))
    return str(value)


def snapshot(instance):
    """Every column of a row as plain JSON values, without the bookkeeping ones."""
    values = {}
    for field in instance._meta.concrete_fields:
        if field.primary_key or field.name in NOISE_FIELDS:
            continue
        values[field.name] = _plain(field.value_from_object(instance))
    return values


def clip(name, value):
    """What goes in the log: secrets hidden, long values shortened."""
    if SENSITIVE.search(name):
        return "[redacted]"
    if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
        return value[:MAX_VALUE_CHARS] + "..."
    if isinstance(value, (dict, list)):
        text = json.dumps(value, default=str)
        if len(text) > MAX_VALUE_CHARS:
            return f"[{len(text)} characters]"
    return value


def _repr(instance):
    try:
        return str(instance)[:200]
    except Exception:  # a __str__ that follows a broken relation must not break the request
        return ""


def build_change(instance, before, after, op):
    if op == "create":
        fields = {k: {"to": clip(k, v)} for k, v in after.items() if v not in (None, "", [], {})}
    elif op == "delete":
        fields = {k: {"from": clip(k, v)} for k, v in before.items() if v not in (None, "", [], {})}
    else:
        fields = {
            k: {"from": clip(k, before.get(k)), "to": clip(k, v)}
            for k, v in after.items() if before.get(k) != v
        }
    return {
        "model": type(instance).__name__,
        "id": str(instance.pk),
        "repr": _repr(instance),
        "op": op,
        "fields": fields,
        "area": _area_of(type(instance)),
    }


def _area_of(model):
    from apps.core.rbac import area_for_model

    return area_for_model(model)


# ---------------------------------------------------------------- recording

def client_ip(request):
    ip = request.META.get("REMOTE_ADDR")
    if getattr(settings, "AUDIT_TRUST_FORWARDED_FOR", False):
        # The proxy in front of us appends the address it saw, so the last entry is the one to trust.
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[-1].strip()
        ip = forwarded or ip
    try:
        return str(ipaddress.ip_address(ip))
    except (ValueError, TypeError):
        return None


def _request_fields(request):
    return {
        "method": request.method[:10],
        "path": request.path[:300],
        "ip_address": client_ip(request),
        "user_agent": request.META.get("HTTP_USER_AGENT", "")[:300],
    }


def record(action, *, request=None, store=None, user=None, email="", resource="", object_id="",
           object_repr="", status_code=None, changes=None, metadata=None):
    """
    Add one line to the audit log. Never raises: a failure to write the log
    must not undo or block the thing that was being logged.
    """
    try:
        if user is not None and not getattr(user, "is_authenticated", True):
            user = None
        return AuditLog.objects.create(
            store=store,
            actor=user,
            actor_email=(getattr(user, "email", "") or email or "")[:254],
            action=action[:80],
            resource=resource[:60],
            object_id=str(object_id)[:64],
            object_repr=object_repr[:200],
            status_code=status_code,
            changes=changes or [],
            metadata=metadata or {},
            **(_request_fields(request) if request is not None else {}),
        )
    except Exception:
        logger.exception("Could not write an audit log entry (%s)", action)
        return None


def log_request(request, response):
    """Called by AuditMiddleware once the response exists. Writes at most one entry."""
    collector = getattr(request, "audit", None)
    if collector is None or collector.store is None:
        return None

    if response.status_code == 403 and collector.denied:
        return record(
            DENIED, request=request, store=collector.store, user=collector.user, resource=collector.area,
            status_code=403,
            metadata={"needed": collector.denied, "attempted": f"{request.method} {request.path}"},
        )

    if request.method not in WRITE_METHODS or response.status_code >= 400:
        return None

    changes = list(collector.changes.values())
    for change in changes:
        # A soft delete is an update of is_deleted; report it as what it is
        if change["op"] == "update" and change["fields"].get("is_deleted", {}).get("to") is True:
            change["op"] = "delete"
    seen = {(c["model"], c["id"]) for c in changes}
    changes += [d for d in collector.deletes if (d["model"], d["id"]) not in seen]

    primary = next((c for c in changes if c["area"] == collector.area), None) or (changes[0] if changes else None)

    metadata = {"omittedChanges": collector.skipped} if collector.skipped else {}
    return record(
        f"{collector.area}.{collector.verb}" if collector.area else collector.verb,
        request=request, store=collector.store, user=collector.user, resource=collector.area,
        object_id=primary["id"] if primary else _url_object_id(request),
        object_repr=primary["repr"] if primary else "",
        status_code=response.status_code, changes=changes, metadata=metadata,
    )


def _url_object_id(request):
    match = getattr(request, "resolver_match", None)
    for value in (match.kwargs.values() if match else ()):
        return str(value)
    return ""
