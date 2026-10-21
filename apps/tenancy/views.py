from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Store
from .serializers import StoreSerializer
from .services import ACTIVE, CLOSED, OWNER, close_store, create_store, delete_store, reopen_store


class StoreViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """
    GET    /api/v1/stores/              stores the signed-in user belongs to
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
        # A platform admin sees every store that isn't closed (and, to reopen one, the closed ones)
        if self.request.user.is_owner:
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

    def perform_update(self, serializer):
        self.check_owner(serializer.instance)
        serializer.save()

    def perform_destroy(self, instance):
        self.check_owner(instance)
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
