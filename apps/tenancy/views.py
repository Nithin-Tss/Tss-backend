import logging

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.rbac import (
    CHECKOUT_MANAGE, DOMAINS_MANAGE, SETTINGS_MANAGE, SHIPPING_MANAGE, TAX_MANAGE,
)

from .access import membership_permissions
from .models import Store
from .serializers import StoreSerializer
from .services import ACTIVE, CLOSED, OWNER, close_store, create_store, delete_store, reopen_store

logger = logging.getLogger(__name__)


_STORE_ID = OpenApiParameter("id", OpenApiTypes.UUID, OpenApiParameter.PATH, description="Store id")


@extend_schema_view(
    retrieve=extend_schema(parameters=[_STORE_ID]),
    partial_update=extend_schema(parameters=[_STORE_ID]),
    destroy=extend_schema(parameters=[_STORE_ID]),
    reopen=extend_schema(parameters=[_STORE_ID]),
    permanent_delete=extend_schema(parameters=[_STORE_ID]),
)
class StoreViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """
    GET    /api/v1/stores/              stores the signed-in user belongs to (only those, even for a platform admin)
    POST   /api/v1/stores/              create a store (the caller becomes its owner)
    GET    /api/v1/stores/<id>/         one of those stores
    PATCH  /api/v1/stores/<id>/         change its settings (that store's owner, or a platform admin)
    DELETE /api/v1/stores/<id>/         close it (that store's owner, or a platform admin)
    POST   /api/v1/stores/<id>/reopen/  reopen a closed store (platform admin only)
    POST   /api/v1/stores/<id>/permanent-delete/  delete a store for good (platform admin only; body
           {"confirmSlug": "<store slug>"}; refused while the store still has products, orders, ...)

    A platform admin (User.is_owner) can manage every store; a store's owner only
    their own. Everyone else gets a 404 for stores they don't belong to.
    """

    serializer_class = StoreSerializer
    permission_classes = [IsAuthenticated]
    # Settings are partial updates; there is no full replace of a store
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):  # OpenAPI schema generation
            return Store.objects.none()
        # The list is always the caller's own stores, platform admin or not. A platform admin can
        # still open, change, close or reopen any store by its id (the platform page lists them all).
        if self.request.user.is_owner and self.action != "list":
            stores = Store.objects.order_by("created_at")
            return stores if self.action in ("reopen", "permanent_delete") else stores.exclude(status=CLOSED)
        # Only the caller's own stores: anyone else's id is a 404
        return Store.objects.filter(
            memberships__user=self.request.user,
            memberships__status=ACTIVE,
        ).order_by("created_at")

    def check_owner(self, store):
        # Settings and closing: this store's owner or a platform admin. Staff never,
        # whatever role they hold, and the check is on the requested store itself.
        if self.request.user.is_owner:
            return
        if not store.memberships.filter(user=self.request.user, status=ACTIVE, role=OWNER).exists():
            raise PermissionDenied("Only the store owner can do this.")

    def perform_create(self, serializer):
        data = serializer.validated_data
        serializer.instance = create_store(
            owner=self.request.user,
            store_name=data["store_name"],
            store_slug=data.get("store_slug"),
        )

    # Settings that a role can be given instead of full ownership; every other field is "settings".
    FIELD_PERMISSIONS = {
        "tax_rate": TAX_MANAGE,
        "shipping_flat_rate": SHIPPING_MANAGE,
        "free_shipping_threshold": SHIPPING_MANAGE,
        "auto_fulfill": CHECKOUT_MANAGE,
        "auto_archive": CHECKOUT_MANAGE,
        "store_slug": DOMAINS_MANAGE,
    }

    def check_settings_access(self, store, fields):
        """Owners may change anything; staff only the settings their roles cover."""
        if self.request.user.is_owner:
            return
        needed = {self.FIELD_PERMISSIONS.get(f, SETTINGS_MANAGE) for f in fields}
        membership = store.memberships.filter(user=self.request.user, status=ACTIVE).first()
        if membership is None or not needed <= membership_permissions(membership):
            raise PermissionDenied("You don't have permission to change these settings.")

    def perform_update(self, serializer):
        self.check_settings_access(serializer.instance, serializer.validated_data)
        serializer.save()

    def perform_destroy(self, instance):
        self.check_owner(instance)
        logger.warning("Store %s (%s) closed by user %s", instance.store_slug, instance.store_id, self.request.user.email)
        close_store(instance)

    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):
        if not request.user.is_owner:
            raise PermissionDenied("Only the administrator can reopen a store.")
        store = self.get_object()
        if store.status == CLOSED:
            reopen_store(store)
        return Response(self.get_serializer(store).data)

    @action(detail=True, methods=["post"], url_path="permanent-delete")
    def permanent_delete(self, request, pk=None):
        # Irreversible, so unlike closing it is not open to a store's owner
        if not request.user.is_owner:
            raise PermissionDenied("Only the administrator can delete a store permanently.")
        store = self.get_object()
        if request.data.get("confirmSlug") != store.store_slug:
            raise ValidationError({"confirmSlug": "Type the store's slug to confirm."})
        delete_store(store)
        return Response(status=204)
