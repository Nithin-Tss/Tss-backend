from apps.core.viewsets import StoreScopedViewSet

from . import services
from .models import Wishlist, WishlistItem
from .serializers import WishlistItemSerializer, WishlistSerializer


class WishlistViewSet(StoreScopedViewSet):
    """/api/v1/wishlists/  ?customer=<id>"""

    queryset = Wishlist.objects.prefetch_related("items__product").order_by("-created_at")
    serializer_class = WishlistSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get("customer"):
            qs = qs.filter(customer=self.request.query_params["customer"])
        return qs


class WishlistItemViewSet(StoreScopedViewSet):
    """/api/v1/wishlists/items/  ?wishlist=<id>"""

    queryset = WishlistItem.objects.select_related("product").order_by("-created_at")
    serializer_class = WishlistItemSerializer
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get("wishlist"):
            qs = qs.filter(wishlist=self.request.query_params["wishlist"])
        return qs

    def perform_create(self, serializer):
        services.add_item(serializer)
