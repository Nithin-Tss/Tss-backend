from django.db import transaction
from rest_framework import mixins
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.core.numbering import next_number
from apps.core.viewsets import StoreScopedViewSet

from . import services
from .models import InventoryItem, Location, Transfer, TransferItem
from .serializers import (
    InventoryItemSerializer,
    LocationSerializer,
    StockChangeSerializer,
    TransferSerializer,
)

PENDING, COMPLETED, CANCELLED = "pending", "completed", "cancelled"


class LocationViewSet(StoreScopedViewSet):
    """/api/v1/inventory/locations/  warehouses, shops, ..."""

    queryset = Location.objects.order_by("name")
    serializer_class = LocationSerializer


class InventoryItemViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    StoreScopedViewSet,
):
    """
    GET  /api/v1/inventory/items/?product=&variant=&location=
    POST /api/v1/inventory/items/set/     {variant, location, available}
    POST /api/v1/inventory/items/adjust/  {variant, location, delta}
    Stock is never edited directly, only through these actions.
    """

    queryset = InventoryItem.objects.select_related("variant__product", "location").order_by(
        "variant__product__title", "location__name"
    )
    serializer_class = InventoryItemSerializer
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        qs = super().get_queryset()
        for param, lookup in (("product", "variant__product"), ("variant", "variant"), ("location", "location")):
            if self.request.query_params.get(param):
                qs = qs.filter(**{lookup: self.request.query_params[param]})
        return qs

    def create(self, request, *args, **kwargs):
        raise ValidationError("Use /set/ or /adjust/ to change stock.")

    def _change(self, request, field):
        data = StockChangeSerializer(data=request.data, context={"request": request})
        data.is_valid(raise_exception=True)
        if field not in data.validated_data:
            raise ValidationError({field: "This field is required."})

        v = data.validated_data
        if field == "available":
            item = services.set_available(v["variant"], v["location"], v["available"])
        else:
            item = services.adjust(v["variant"], v["location"], v["delta"])
        return Response(InventoryItemSerializer(item).data)

    @action(detail=False, methods=["post"])
    def set(self, request):
        return self._change(request, "available")

    @action(detail=False, methods=["post"])
    def adjust(self, request):
        return self._change(request, "delta")


class TransferViewSet(StoreScopedViewSet):
    """
    /api/v1/inventory/transfers/                      list, create, retrieve
    POST /api/v1/inventory/transfers/<id>/complete/   moves the stock
    POST /api/v1/inventory/transfers/<id>/cancel/
    """

    queryset = Transfer.objects.prefetch_related("items__variant").order_by("-created_at")
    serializer_class = TransferSerializer
    http_method_names = ["get", "post", "head", "options"]

    @transaction.atomic
    def perform_create(self, serializer):
        items = serializer.validated_data.pop("items")
        transfer = serializer.save(
            store=self.request.store,
            status=PENDING,
            transfer_number=next_number(self.request.store, Transfer, "transfer_number", "T-"),
        )
        TransferItem.objects.bulk_create(
            [TransferItem(transfer=transfer, **item) for item in items]
        )

    def _pending(self):
        transfer = self.get_object()
        if transfer.status != PENDING:
            raise ValidationError(f"This transfer is already {transfer.status}.")
        return transfer

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        with transaction.atomic():
            transfer = self._pending()
            for item in transfer.items.select_related("variant__product"):
                services.move(
                    item.variant, transfer.source_location, transfer.destination_location, item.quantity
                )
            transfer.status = COMPLETED
            transfer.save(update_fields=["status", "updated_at"])
        return Response(self.get_serializer(transfer).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        transfer = self._pending()
        transfer.status = CANCELLED
        transfer.save(update_fields=["status", "updated_at"])
        return Response(self.get_serializer(transfer).data)
