"""
Store-facing endpoints:

    /api/v1/api-keys/        the store's API keys (api_keys.view / api_keys.manage, feature flag "api_keys")
    /api/v1/feature-flags/   which feature flags are on for this store (any member)
"""
import secrets

from django.db import IntegrityError, transaction
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import HasStorePermission, IsStoreMember
from apps.core.viewsets import StoreScopedViewSet

from . import flags, scopes
from .authentication import KEY_PREFIX, hash_secret
from .models import ApiKey
from .permissions import feature_required
from .serializers import MAX_ACTIVE_KEYS, ApiKeySerializer


def new_secret():
    """(prefix, full key). The full key is shown to the person once and never stored."""
    prefix = secrets.token_hex(4)
    return prefix, f"{KEY_PREFIX}{prefix}_{secrets.token_urlsafe(32)}"


class ApiKeyViewSet(StoreScopedViewSet):
    """
    GET    /api/v1/api-keys/              the store's keys (never their secrets)
    POST   /api/v1/api-keys/              {name, scopes, expiresAt?}: make a key; the response holds "key", shown once
    GET    /api/v1/api-keys/scopes/       what a key can be given, grouped
    PATCH  /api/v1/api-keys/<id>/         rename, change scopes or expiry
    POST   /api/v1/api-keys/<id>/rotate/  a new secret for the same key; the old one stops working at once
    POST   /api/v1/api-keys/<id>/revoke/  stop the key for good (the row stays for the record)
    DELETE /api/v1/api-keys/<id>/         remove the key's row

    Send a key as "Authorization: Bearer tssk_..." or "X-Api-Key: tssk_...".
    """

    queryset = ApiKey.objects.select_related("store", "created_by")
    serializer_class = ApiKeySerializer
    permission_classes = [IsAuthenticated, IsStoreMember, HasStorePermission, feature_required("api_keys")]
    permission_resource = "api_keys"
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def _with_secret(self, key, secret, status=200):
        return Response({**self.get_serializer(key).data, "key": secret}, status=status)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if self.get_queryset().filter(revoked_at__isnull=True).count() >= MAX_ACTIVE_KEYS:
            raise ValidationError({"detail": f"A store can have at most {MAX_ACTIVE_KEYS} API keys. Revoke one first."})

        for _ in range(5):  # the prefix is unique; a clash is a one-in-4-billion event
            prefix, secret = new_secret()
            try:
                with transaction.atomic():
                    key = serializer.save(
                        store=request.store, created_by=request.user, prefix=prefix, secret_hash=hash_secret(secret),
                    )
                break
            except IntegrityError:
                continue
        else:
            raise ValidationError({"detail": "Could not generate a key. Try again."})
        return self._with_secret(key, secret, status=201)

    def update(self, request, *args, **kwargs):
        key = self.get_object()
        if key.revoked_at:
            raise ValidationError({"detail": "A revoked key can't be changed."})
        return super().update(request, *args, **kwargs)

    @action(detail=False, methods=["get"], url_path="scopes", action_permissions={"GET": "api_keys.manage"})
    def scope_list(self, request):
        return Response(scopes.catalog())

    @action(detail=True, methods=["post"])
    def rotate(self, request, pk=None):
        key = self.get_object()
        if key.status != "active":
            raise ValidationError({"detail": f"A {key.status} key can't be rotated. Make a new one."})
        prefix, secret = new_secret()
        key.prefix, key.secret_hash = prefix, hash_secret(secret)
        key.save(update_fields=["prefix", "secret_hash", "updated_at"])
        return self._with_secret(key, secret)

    @action(detail=True, methods=["post"])
    def revoke(self, request, pk=None):
        key = self.get_object()
        if not key.revoked_at:
            key.revoked_at = timezone.now()
            key.save(update_fields=["revoked_at", "updated_at"])
        return Response(self.get_serializer(key).data)


class StoreFeatureFlagsView(APIView):
    """GET /api/v1/feature-flags/  {"flags": {"api_keys": true, ...}} for the current store (any member)"""

    permission_classes = [IsAuthenticated, IsStoreMember, HasStorePermission]
    permission_resource = "settings"
    action_permissions = {"GET": None}

    @extend_schema(responses={200: dict})
    def get(self, request):
        return Response({"flags": flags.flags_for_store(request.store)})
