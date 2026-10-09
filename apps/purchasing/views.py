from django.db import transaction
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.core.numbering import next_number
from apps.core.viewsets import StoreScopedViewSet
from apps.inventory import services as inventory

from .models import PurchaseOrder, PurchaseOrderItem
from .serializers import PurchaseOrderSerializer, ReceiveSerializer

DRAFT, ORDERED, RECEIVED, CANCELLED = "draft", "ordered", "received", "cancelled"


class PurchaseOrderViewSet(StoreScopedViewSet):
    """
    /api/v1/purchase-orders/                       buying stock from suppliers
    POST /api/v1/purchase-orders/<id>/order/       mark as sent to the supplier
    POST /api/v1/purchase-orders/<id>/receive/     {location}: adds the items to stock
    POST /api/v1/purchase-orders/<id>/cancel/
    Only drafts can be edited or deleted.
    """

    queryset = PurchaseOrder.objects.prefetch_related("items__variant__product").order_by("-created_at")
    serializer_class = PurchaseOrderSerializer

    def _replace_items(self, po, items):
        po.items.all().delete()
        PurchaseOrderItem.objects.bulk_create(
            [PurchaseOrderItem(purchase_order=po, **item) for item in items]
        )

    @transaction.atomic
    def perform_create(self, serializer):
        items = serializer.validated_data.pop("items")
        po = serializer.save(
            store=self.request.store,
            status=DRAFT,
            po_number=next_number(self.request.store, PurchaseOrder, "po_number", "PO-"),
        )
        self._replace_items(po, items)

    @transaction.atomic
    def perform_update(self, serializer):
        if serializer.instance.status != DRAFT:
            raise ValidationError("Only draft purchase orders can be changed.")
        items = serializer.validated_data.pop("items", None)
        po = serializer.save()
        if items is not None:
            self._replace_items(po, items)

    def perform_destroy(self, instance):
        if instance.status != DRAFT:
            raise ValidationError("Only draft purchase orders can be deleted. Cancel it instead.")
        with transaction.atomic():
            instance.items.all().delete()
            instance.delete()

    def _move(self, allowed, new_status):
        po = self.get_object()
        if po.status not in allowed:
            raise ValidationError(f"A {po.status} purchase order can't become {new_status}.")
        po.status = new_status
        po.save(update_fields=["status", "updated_at"])
        return po

    @action(detail=True, methods=["post"])
    def order(self, request, pk=None):
        return Response(self.get_serializer(self._move((DRAFT,), ORDERED)).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        return Response(self.get_serializer(self._move((DRAFT, ORDERED), CANCELLED)).data)

    @action(detail=True, methods=["post"])
    def receive(self, request, pk=None):
        data = ReceiveSerializer(data=request.data, context={"request": request})
        data.is_valid(raise_exception=True)
        location = data.validated_data["location"]

        with transaction.atomic():
            po = self._move((DRAFT, ORDERED), RECEIVED)
            for item in po.items.select_related("variant"):
                inventory.receive(item.variant, location, item.quantity)

        return Response(self.get_serializer(po).data)
