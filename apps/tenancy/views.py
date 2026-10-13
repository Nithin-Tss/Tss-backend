from rest_framework import mixins, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated

from .models import Store
from .serializers import StoreSerializer
from .services import ACTIVE, CLOSED, OWNER, close_store, create_store


class StoreViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """
    GET    /api/v1/stores/       stores the signed-in user belongs to
    POST   /api/v1/stores/       create a store (the caller becomes its owner)
    GET    /api/v1/stores/<id>/  one of those stores
    PATCH  /api/v1/stores/<id>/  change its settings (root admin only)
    DELETE /api/v1/stores/<id>/  close it (root admin only)
    """

    serializer_class = StoreSerializer
    permission_classes = [IsAuthenticated]
    # Renames are partial updates; there is no full replace of a store
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        # A platform admin sees every store that isn't closed
        if self.request.user.is_owner:
            return Store.objects.exclude(status=CLOSED).order_by("created_at")
        # Only the caller's own stores: anyone else's id is a 404
        return Store.objects.filter(
            memberships__user=self.request.user,
            memberships__status=ACTIVE,
        ).order_by("created_at")

    def check_owner(self, store):
        # Store settings (name, URL, field names) and closing a store: root admin only
        if not self.request.user.is_owner:
            raise PermissionDenied("Only the administrator can change store settings.")

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
