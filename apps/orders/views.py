from decimal import Decimal

from django.db import transaction
from rest_framework import mixins, status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.core.numbering import next_number
from apps.core.viewsets import StoreScopedViewSet

from . import services
from .models import DraftOrder, DraftOrderDetail, Order
from .serializers import DraftOrderSerializer, OrderCreateSerializer, OrderSerializer

OPEN_DRAFT, COMPLETED_DRAFT = "open", "completed"


class OrderViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    StoreScopedViewSet,
):
    """
    /api/v1/orders/            list (?status=, ?fulfillment_status=, ?customer=), create a manual order
    /api/v1/orders/<id>/       retrieve, PATCH delivery_method
    POST /api/v1/orders/<id>/cancel/   cancel and put the stock back
    Orders are never deleted - cancel them instead.
    """

    queryset = Order.objects.select_related("customer").prefetch_related("items").order_by("-created_at")
    serializer_class = OrderSerializer
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        qs = super().get_queryset()
        for param in ("status", "fulfillment_status", "customer"):
            if self.request.query_params.get(param):
                qs = qs.filter(**{param: self.request.query_params[param]})
        return qs

    def create(self, request, *args, **kwargs):
        data = OrderCreateSerializer(data=request.data, context={"request": request})
        data.is_valid(raise_exception=True)
        v = dict(data.validated_data)
        order = services.create_order(
            request.store,
            v.pop("items"),
            currency=v.pop("currency_code"),
            **v,
        )
        return Response(OrderSerializer(order).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        order = services.cancel_order(self.get_object())
        return Response(OrderSerializer(order).data)


class DraftOrderViewSet(StoreScopedViewSet):
    """
    /api/v1/orders/drafts/                       quotes / orders being prepared
    POST /api/v1/orders/drafts/<id>/complete/    turn the draft into a real order
    """

    queryset = DraftOrder.objects.prefetch_related("details").select_related("created_by_user").order_by("-created_at")
    serializer_class = DraftOrderSerializer

    def _save_details(self, draft, details):
        draft.details.all().delete()
        total = Decimal("0.00")

        for d in details:
            product, variant = d.get("product"), d.get("variant")
            if variant is not None and product is not None and variant.product_id != product.pk:
                raise ValidationError({"details": "A variant doesn't match its product."})
            if product is not None and variant is None:
                variant = product.variants.order_by("created_at").first()
            if product is None and not d.get("title"):
                raise ValidationError({"details": "Custom items need a title."})

            unit_price = d.get("unit_price")
            if unit_price is None:
                unit_price = variant.price if variant else Decimal("0.00")
            discount = d.get("discount_amount") or Decimal("0.00")
            line_total = max(unit_price * d["quantity"] - discount, Decimal("0.00"))

            DraftOrderDetail.objects.create(
                draft_order=draft,
                product=product,
                variant=variant,
                title=d.get("title") or (product.title if product else ""),
                quantity=d["quantity"],
                unit_price=unit_price,
                discount_amount=discount,
                total=line_total,
            )
            total += line_total

        draft.total = total
        draft.save(update_fields=["total", "updated_at"])

    @transaction.atomic
    def perform_create(self, serializer):
        details = serializer.validated_data.pop("details")
        draft = serializer.save(
            store=self.request.store,
            created_by_user=self.request.user,
            status=OPEN_DRAFT,
            total=Decimal("0.00"),
            draft_number=next_number(self.request.store, DraftOrder, "draft_number", "D-"),
        )
        self._save_details(draft, details)

    @transaction.atomic
    def perform_update(self, serializer):
        if serializer.instance.status != OPEN_DRAFT:
            raise ValidationError("Completed drafts can't be changed.")
        details = serializer.validated_data.pop("details", None)
        draft = serializer.save()
        if details is not None:
            self._save_details(draft, details)

    def perform_destroy(self, instance):
        if instance.status != OPEN_DRAFT:
            raise ValidationError("Completed drafts can't be deleted.")
        with transaction.atomic():
            instance.details.all().delete()
            instance.delete()

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        with transaction.atomic():
            draft = DraftOrder.objects.select_for_update().get(pk=self.get_object().pk)
            if draft.status != OPEN_DRAFT:
                raise ValidationError("This draft is already completed.")

            lines = [
                {
                    "product": d.product,
                    "variant": d.variant,
                    "quantity": d.quantity,
                    "unit_price": d.unit_price,
                    "discount_amount": d.discount_amount,
                }
                for d in draft.details.select_related("product", "variant")
            ]
            if any(line["product"] is None for line in lines):
                raise ValidationError("Custom items (without a product) can't be ordered yet.")

            order = services.create_order(
                request.store, lines, customer=draft.customer, currency=draft.currency_code
            )
            draft.order = order
            draft.status = COMPLETED_DRAFT
            draft.save(update_fields=["order", "status", "updated_at"])

        return Response(OrderSerializer(order).data, status=status.HTTP_201_CREATED)
