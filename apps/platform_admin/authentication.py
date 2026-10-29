"""
API key authentication.

    Authorization: Bearer tssk_<prefix>_<secret>        or        X-Api-Key: tssk_<prefix>_<secret>

The request then acts in the key's store with only the key's scopes (see
apps/core/permissions.py IsStoreMember). Keys are limited to the store's own
data endpoints below: never sign-in, account, team or platform endpoints.
"""
import hashlib
import re
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from rest_framework import authentication, exceptions

from apps.audit import services as audit

from .models import ApiKey

KEY_PREFIX = "tssk_"
KEY_PATTERN = re.compile(r"^tssk_([A-Za-z0-9]{8})_([A-Za-z0-9_-]{20,})$")

# Where a key may be used: the store's business data.
ALLOWED_PATHS = re.compile(
    r"^/api/v1/(catalog|customers|orders|inventory|discounts|gift-cards|fulfillments|delivery|"
    r"wishlists|purchase-orders|reports|carts|checkouts|media)/"
)
TOUCH_EVERY = timedelta(minutes=1)


def hash_secret(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def _raw_key(request):
    header = request.headers.get("X-Api-Key", "").strip()
    if header:
        return header
    parts = request.headers.get("Authorization", "").split()
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].startswith(KEY_PREFIX):
        return parts[1]
    return ""


def _rate_limit(key):
    limit = settings.API_KEY_RATE_LIMIT_PER_MINUTE
    if not limit:
        return
    bucket = f"apikey-rate:{key.pk}:{int(timezone.now().timestamp() // 60)}"
    cache.add(bucket, 0, 90)
    try:
        used = cache.incr(bucket)
    except ValueError:
        used = 1
    if used > limit:
        raise exceptions.Throttled(detail="API key rate limit exceeded.")


class ApiKeyAuthentication(authentication.BaseAuthentication):
    def authenticate_header(self, request):
        return "Bearer"

    def authenticate(self, request):
        raw = _raw_key(request)
        if not raw:
            return None  # not an API key request: the next authenticator decides

        match = KEY_PATTERN.match(raw)
        key = ApiKey.objects.select_related("store", "created_by").filter(prefix=match.group(1)).first() if match else None
        if key is None or not constant_time_compare(key.secret_hash, hash_secret(raw)):
            raise exceptions.AuthenticationFailed("Invalid API key.")
        if key.created_by is None:
            raise exceptions.AuthenticationFailed("This API key is no longer usable.")
        if key.status != "active":
            raise exceptions.AuthenticationFailed(f"This API key is {key.status}.")
        if key.store.status != "active":
            raise exceptions.PermissionDenied("This store is not active.")
        if not ALLOWED_PATHS.match(request.path):
            raise exceptions.PermissionDenied("API keys can't be used for this endpoint.")

        _rate_limit(key)
        self._touch(key, request)
        return key.created_by, key

    @staticmethod
    def _touch(key, request):
        now = timezone.now()
        if key.last_used_at and now - key.last_used_at < TOUCH_EVERY:
            return
        # update(), not save(): the audit log must not record "last used" as a change
        ApiKey.objects.filter(pk=key.pk).update(last_used_at=now, last_used_ip=audit.client_ip(request))
