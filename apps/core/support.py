"""
POST /api/v1/support/contact/   {subject, message, transcript?, page?}  ->  {sent: true}

Emails one plain-text message to settings.SUPPORT_EMAIL, reply-to the signed-in user. Any active member of the
store may send one, whatever their role (suspended stores are refused by IsStoreMember, like every other store API). Nothing is stored:
no model, and the request is taken out of the audit log (see SupportContactView.post).

Store and user details come from the request, never from the body.
"""
import logging
import re

from django.conf import settings
from rest_framework import exceptions, status
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from apps.core.permissions import IsStoreMember
from apps.core.tasks import send_store_email
from apps.platform_admin.models import ApiKey

logger = logging.getLogger(__name__)

MAX_SUBJECT = 150
MIN_MESSAGE = 10
MAX_MESSAGE = 2000
MAX_TRANSCRIPT = 4000
MAX_PAGE = 200


class NoApiKeys(BasePermission):
    message = "Support requests can't be sent with an API key."

    def has_permission(self, request, view):
        return not isinstance(request.auth, ApiKey)


class SupportThrottle(SimpleRateThrottle):
    """5 per hour per signed-in user (rate: DEFAULT_THROTTLE_RATES["support_contact"])."""

    scope = "support_contact"

    def allow_request(self, request, view):
        # Not configured: the view answers 503 and must not use up the user's allowance
        if not settings.SUPPORT_EMAIL:
            return True
        return super().allow_request(request, view)

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": request.user.pk}


def _text(data, field, *, required, max_length, min_length=0, single_line=False):
    value = data.get(field)
    if value is None or value == "":
        if required:
            raise exceptions.ValidationError({field: "This field is required."})
        return ""
    if not isinstance(value, str):
        raise exceptions.ValidationError({field: "Must be text."})
    value = re.sub(r"\s+", " ", value) if single_line else value.replace("\r\n", "\n").replace("\r", "\n")
    value = value.strip()
    if required and not value:
        raise exceptions.ValidationError({field: "This field is required."})
    if len(value) > max_length:
        raise exceptions.ValidationError({field: f"Keep it under {max_length + 1} characters."})
    if value and len(value) < min_length:
        raise exceptions.ValidationError({field: f"Write at least {min_length} characters."})
    return value


def clean_request(data):
    """The validated fields as a dict. Raises ValidationError({field: message})."""
    if not isinstance(data, dict):
        raise exceptions.ValidationError({"detail": "Send a JSON object."})
    return {
        "subject": _text(data, "subject", required=True, max_length=MAX_SUBJECT, single_line=True),
        "message": _text(data, "message", required=True, max_length=MAX_MESSAGE, min_length=MIN_MESSAGE),
        "transcript": _text(data, "transcript", required=False, max_length=MAX_TRANSCRIPT),
        "page": _text(data, "page", required=False, max_length=MAX_PAGE, single_line=True),
    }


def build_support_email(*, fields, store, user):
    """(subject, body) of the email. Store and user come from the request, not from `fields`."""
    name = user.get_full_name() or "-"
    store_name = " ".join(str(store.store_name).split())
    subject = f"[Support] {store_name}: {fields['subject']}"
    parts = [
        fields["message"],
        "\n".join([
            "--- Details ---",
            f"Store: {store_name}",
            f"Store ID: {store.store_id}",
            f"Name: {name}",
            f"Email: {user.email}",
            f"Page I came from: {fields['page'] or '-'}",
        ]),
    ]
    if fields["transcript"]:
        parts.append(f"--- Chat with the assistant ---\n{fields['transcript']}")
    return subject, "\n\n".join(parts)


class SupportContactView(APIView):
    # Any active member of the store, whatever their role; same list as the AI assistant (see RouteAuditTest)
    permission_classes = [IsAuthenticated, NoApiKeys, IsStoreMember]
    throttle_classes = [SupportThrottle]

    def post(self, request):
        # IsStoreMember registered this request for auditing, which would write an AuditLog row for a successful
        # POST. A support message changes nothing, so drop it: log_request() returns early without a collector.
        request._request.audit = None

        if not settings.SUPPORT_EMAIL:
            return Response({"detail": "Support email is not configured."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        fields = clean_request(request.data)
        subject, body = build_support_email(fields=fields, store=request.store, user=request.user)
        try:
            send_store_email(subject, body, [settings.SUPPORT_EMAIL], reply_to=request.user.email)
        except Exception:
            logger.exception("Support email could not be sent (store %s)", request.store.store_id)
            return Response(
                {"detail": "We couldn't send your message. Please try again."}, status=status.HTTP_502_BAD_GATEWAY
            )
        return Response({"sent": True})
