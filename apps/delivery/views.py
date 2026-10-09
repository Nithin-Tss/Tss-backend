from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.viewsets import StoreScopedGenericViewSet, StoreScopedViewSet

from . import services
from .models import DeliveryGateway, Shipment
from .serializers import (
    DeliveryGatewaySerializer,
    ShipmentSerializer,
    ShipSerializer,
    TrackingEventSerializer,
)


class DeliveryGatewayViewSet(StoreScopedViewSet):
    """/api/v1/delivery/gateways/  delivery partners (courier companies, own van, ...)"""

    queryset = DeliveryGateway.objects.order_by("name")
    serializer_class = DeliveryGatewaySerializer


class ShipmentViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    StoreScopedGenericViewSet,
):
    """
    GET  /api/v1/delivery/shipments/?order=<id>
    POST /api/v1/delivery/shipments/                {fulfillment, delivery_gateway, tracking_number}
    POST /api/v1/delivery/shipments/<id>/events/    {status, location, description}  ("delivered" completes it)
    """

    queryset = Shipment.objects.select_related("delivery_gateway", "fulfillment").prefetch_related(
        "items__order_item", "tracking_events"
    ).order_by("-created_at")
    serializer_class = ShipmentSerializer
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get("order"):
            qs = qs.filter(fulfillment__order=self.request.query_params["order"])
        return qs

    def create(self, request, *args, **kwargs):
        data = ShipSerializer(data=request.data, context={"request": request})
        data.is_valid(raise_exception=True)
        v = data.validated_data
        shipment = services.ship(
            request.store, v["fulfillment"], v["delivery_gateway"], v.get("tracking_number") or None
        )
        return Response(
            ShipmentSerializer(self.get_queryset().get(pk=shipment.pk)).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def events(self, request, pk=None):
        data = TrackingEventSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        shipment = self.get_object()
        services.add_event(shipment, **data.validated_data)
        return Response(ShipmentSerializer(self.get_queryset().get(pk=shipment.pk)).data)
