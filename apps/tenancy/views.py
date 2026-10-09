from rest_framework import mixins, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated

from .models import Store
from .serializers import StoreSerializer
from .services import ACTIVE, OWNER, close_store, create_store


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
    PATCH  /api/v1/stores/<id>/  rename it (owners only)
    DELETE /api/v1/stores/<id>/  close it (owners only)
    """

    serializer_class = StoreSerializer
    permission_classes = [IsAuthenticated]
    # Renames are partial updates; there is no full replace of a store
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        # Only the caller's own stores: anyone else's id is a 404
        return Store.objects.filter(
            memberships__user=self.request.user,
            memberships__status=ACTIVE,
        ).order_by("created_at")

    def check_owner(self, store):
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
