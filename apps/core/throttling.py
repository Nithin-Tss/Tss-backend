"""
Default rate limit for the staff/admin API (everything that doesn't set its own throttle).

Signed-in requests are counted per user, anything else per client IP. API-key requests are
skipped: they already have their own per-key limit (apps/platform_admin/authentication.py).
"""
from rest_framework.throttling import SimpleRateThrottle

from apps.platform_admin.models import ApiKey


class DefaultApiThrottle(SimpleRateThrottle):
    scope = "api"

    def get_cache_key(self, request, view):
        if isinstance(request.auth, ApiKey):
            return None
        user = request.user
        ident = user.pk if user and user.is_authenticated else self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}
