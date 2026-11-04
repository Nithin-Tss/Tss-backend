"""
Origin check for the sign-in, sign-up and sign-out endpoints.

They skip DRF authentication (so a dead token can still sign out), which also skips the CSRF check
SessionAuthentication would run. They still set or clear the session cookie, so a page on another site
must not be able to post to them from a visitor's browser (forced sign-in to the attacker's account,
forced sign-out). Browsers always send Origin on such a post; scripts and mobile apps send none and pass.
"""
from urllib.parse import urlsplit

from django.conf import settings
from rest_framework.permissions import BasePermission


def _origin_allowed(request):
    origin = request.headers.get("Origin")
    if not origin:
        return True
    trusted = {*settings.CORS_ALLOWED_ORIGINS, *settings.CSRF_TRUSTED_ORIGINS}
    parts = urlsplit(origin)
    return origin in trusted or (parts.scheme in ("http", "https") and parts.netloc == request.get_host())


class TrustedOrigin(BasePermission):
    message = "Request origin is not allowed."

    def has_permission(self, request, view):
        return _origin_allowed(request)
