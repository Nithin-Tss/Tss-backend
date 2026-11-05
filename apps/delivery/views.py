from rest_framework import mixins, status
from rest_framework.decorators import action
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated

from apps.core.permissions import HasStorePermission, IsStoreMember
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.viewsets import StoreScopedGenericViewSet, StoreScopedViewSet
from apps.tenancy.models import ShippingRate, ShippingZone, TaxRate

from . import providers, services
from .models import DeliveryGateway, Shipment
from .serializers import (
    DeliveryGatewaySerializer,
    ShippingRateSerializer,
    ShippingZoneSerializer,
    TaxRateSerializer,
    ShipmentSerializer,
    ShipSerializer,
    TrackingEventSerializer,
)


class DeliveryGatewayViewSet(StoreScopedViewSet):
    """/api/v1/delivery/gateways/  delivery partners (courier companies, own van, ...)"""

    permission_resource = "shipping"
    queryset = DeliveryGateway.objects.order_by("name")
    serializer_class = DeliveryGatewaySerializer


class ShippingRateViewSet(StoreScopedViewSet):
    """/api/v1/delivery/rates/  checkout shipping fees by destination country and cart weight"""

    permission_resource = "shipping"
    queryset = ShippingRate.objects.order_by("country", "min_weight")
    serializer_class = ShippingRateSerializer


class ShippingZoneViewSet(StoreScopedViewSet):
    """/api/v1/delivery/zones/  named groups of countries and regions that rates can target"""

    permission_resource = "shipping"
    queryset = ShippingZone.objects.prefetch_related("locations").order_by("name")
    serializer_class = ShippingZoneSerializer


class TaxRateViewSet(StoreScopedViewSet):
    """/api/v1/delivery/tax-rates/  tax percentages by country and region (Store.tax_rate is the fallback)"""

    permission_resource = "shipping"
    queryset = TaxRate.objects.order_by("country", "region")
    serializer_class = TaxRateSerializer


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

    permission_resource = "fulfillment"
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
        driver = {"name": v.get("driver_name"), "phone": v.get("driver_phone"), "eta": v["eta"].isoformat() if v.get("eta") else None}
        parcel = {k: float(v[k]) for k in ("weight", "length", "breadth", "height") if k in v}
        shipment = services.ship(
            request.store, v["fulfillment"], v["delivery_gateway"], v.get("tracking_number") or None,
            driver=driver, parcel=parcel,
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


class CarrierWebhookView(APIView):
    """
    POST /api/v1/delivery/webhooks/<gateway_id>/   a courier reporting parcel status.
    Public URL, but every request must carry the secret stored on that gateway, so only the courier can use it.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, gateway_id):
        gateway = get_object_or_404(DeliveryGateway, pk=gateway_id, is_active=True, is_deleted=False, kind="carrier")
        provider = providers.get_provider(gateway.provider)
        try:
            provider.verify_webhook(gateway, request)
        except ValidationError:
            return Response(status=status.HTTP_401_UNAUTHORIZED)
        applied = services.apply_carrier_events(gateway, provider.parse_webhook(request.data))
        return Response({"applied": applied})


class ProviderListView(APIView):
    """GET /api/v1/delivery/providers/  the delivery options an owner can set up, and what each needs."""

    permission_classes = [IsAuthenticated, IsStoreMember, HasStorePermission]
    permission_resource = "shipping"

    def get(self, request):
        return Response([
            {"code": p.code, "label": p.label, "kind": p.kind, "credential_fields": list(p.credential_fields), "fields": list(p.fields)}
            for p in providers.REGISTRY.values()
        ])
