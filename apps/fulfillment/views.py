from rest_framework import mixins, status
from rest_framework.response import Response

from apps.core.viewsets import StoreScopedGenericViewSet

from . import services
from .models import Fulfillment
from .serializers import FulfillmentSerializer, FulfillSerializer


class FulfillmentViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    StoreScopedGenericViewSet,
):
    """
    GET  /api/v1/fulfillments/?order=<id>
    POST /api/v1/fulfillments/   {order, items: [{order_item, quantity}]}  (no items = everything left)
    """

    queryset = Fulfillment.objects.select_related("order").prefetch_related("items__order_item").order_by("-created_at")
    serializer_class = FulfillmentSerializer
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get("order"):
            qs = qs.filter(order=self.request.query_params["order"])
        return qs

    def create(self, request, *args, **kwargs):
        data = FulfillSerializer(data=request.data, context={"request": request})
        data.is_valid(raise_exception=True)
        v = data.validated_data
        fulfillment = services.fulfill(v["order"], v.get("items"), v.get("delivery_method") or None)
        return Response(
            FulfillmentSerializer(self.get_queryset().get(pk=fulfillment.pk)).data,
            status=status.HTTP_201_CREATED,
        )
