from rest_framework import mixins, viewsets
from rest_framework.permissions import IsAuthenticated

from .models import Store
from .serializers import StoreSerializer
from .services import ACTIVE, create_store


class StoreViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """
    GET  /api/v1/stores/       stores the signed-in user belongs to
    POST /api/v1/stores/       create a store (the caller becomes its owner)
    GET  /api/v1/stores/<id>/  one of those stores
    """

    serializer_class = StoreSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Store.objects.filter(
            memberships__user=self.request.user,
            memberships__status=ACTIVE,
        ).order_by("created_at")

    def perform_create(self, serializer):
        data = serializer.validated_data
        serializer.instance = create_store(
            owner=self.request.user,
            store_name=data["store_name"],
            store_slug=data.get("store_slug"),
        )
