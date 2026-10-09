from apps.core.viewsets import StoreScopedViewSet

from .models import Cart, Checkout
from .serializers import CartSerializer, CheckoutSerializer


class CartViewSet(StoreScopedViewSet):
    """
    GET /api/v1/carts/?status=   shoppers' carts (read-only for staff).
    Shoppers create and change carts through the storefront cart API (next phase).
    """

    queryset = Cart.objects.prefetch_related("items__product").order_by("-updated_at")
    serializer_class = CartSerializer
    http_method_names = ["get", "head", "options"]

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get("status"):
            qs = qs.filter(status=self.request.query_params["status"])
        return qs


class CheckoutViewSet(StoreScopedViewSet):
    """GET /api/v1/checkouts/?abandoned=true   (read-only for staff)"""

    queryset = Checkout.objects.prefetch_related("items").order_by("-updated_at")
    serializer_class = CheckoutSerializer
    http_method_names = ["get", "head", "options"]

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get("abandoned") == "true":
            qs = qs.filter(abandoned_at__isnull=False)
        if self.request.query_params.get("status"):
            qs = qs.filter(status=self.request.query_params["status"])
        return qs
