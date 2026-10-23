from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import ProtectedError
from rest_framework import mixins, status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.core.numbering import next_number
from apps.core.viewsets import StoreScopedGenericViewSet, StoreScopedViewSet

from . import services
from apps.tenancy.models import Store

from .models import DraftOrder, DraftOrderDetail, Order, OrderIdempotencyKey
from .serializers import DraftOrderSerializer, OrderCreateSerializer, OrderSerializer

OPEN_DRAFT, COMPLETED_DRAFT = "open", "completed"


def filter_by_params(qs, query_params, params):
    """Apply ?param= filters; a malformed id (e.g. ?customer=abc) is a 400, not a 500."""
    for param in params:
        if query_params.get(param):
            try:
                qs = qs.filter(**{param: query_params[param]})
            except DjangoValidationError:
                raise ValidationError({param: "Enter a valid id."})
    return qs


class OrderViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    StoreScopedGenericViewSet,
):
    """
    /api/v1/orders/            list (?status=, ?fulfillment_status=, ?customer=), create a manual order
    /api/v1/orders/<id>/       retrieve, PATCH delivery_method
    POST /api/v1/orders/<id>/cancel/   cancel and put the stock back
    Orders are never deleted - cancel them instead.
    """

    permission_resource = "orders"
    queryset = (
        Order.objects.select_related("customer", "sales_channel")
        .prefetch_related(
            "items__product__images",
            "items__variant",
            "payments__transactions",
            "fulfillments__items__order_item",
            "source_draft__created_by_user",
        )
        .order_by("-created_at")
    )
    serializer_class = OrderSerializer
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        return filter_by_params(
            super().get_queryset(), self.request.query_params, ("status", "fulfillment_status", "customer")
        )

    def create(self, request, *args, **kwargs):
        data = OrderCreateSerializer(data=request.data, context={"request": request})
        data.is_valid(raise_exception=True)
        v = dict(data.validated_data)

        # An Idempotency-Key header makes a retried request return the first order instead of a second one.
        key = (request.headers.get("Idempotency-Key") or "").strip()
        if len(key) > 100:
            raise ValidationError({"Idempotency-Key": "Must be at most 100 characters."})

        with transaction.atomic():
            if key:
                # The store row lock serialises concurrent requests carrying the same key.
                Store.objects.select_for_update().filter(pk=request.store.pk).first()
                seen = OrderIdempotencyKey.objects.filter(store=request.store, key=key).select_related("order").first()
                if seen is not None:
                    return Response(OrderSerializer(seen.order).data, status=status.HTTP_200_OK)

            order = services.create_order(
                request.store,
                v.pop("items"),
                currency=v.pop("currency_code", None),
                user=request.user,
                **v,
            )
            if key:
                OrderIdempotencyKey.objects.create(store=request.store, key=key, order=order)
        return Response(OrderSerializer(order).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        order = services.cancel_order(self.get_object(), user=request.user)
        return Response(OrderSerializer(order).data)


class DraftOrderViewSet(StoreScopedViewSet):
    """
    /api/v1/orders/drafts/                       quotes / orders being prepared
    POST /api/v1/orders/drafts/<id>/complete/    turn the draft into a real order
    """

    permission_resource = "draft_orders"
    queryset = (
        DraftOrder.objects.prefetch_related("details")
        .select_related("created_by_user", "customer")
        .order_by("-created_at")
    )
    serializer_class = DraftOrderSerializer

    def get_queryset(self):
        return filter_by_params(super().get_queryset(), self.request.query_params, ("status", "customer"))

    def _save_details(self, draft, details):
        draft.details.all().delete()
        total = Decimal("0.00")

        for d in details:
            product, variant = d.get("product"), d.get("variant")
            if product is not None and product.store_id != draft.store_id:
                raise ValidationError({"details": "A product doesn't belong to this store."})
            if variant is not None and product is None:
                raise ValidationError({"details": "A product is required when a variant is selected."})
            if variant is not None and variant.product_id != product.pk:
                raise ValidationError({"details": "A variant doesn't match its product."})
            if variant is not None and variant.product.store_id != draft.store_id:
                raise ValidationError({"details": "A variant doesn't belong to this store."})
            if product is not None and variant is None:
                variant = product.variants.order_by("created_at").first()
            if product is None and not d.get("title", "").strip():
                raise ValidationError({"details": "Custom items need a title."})

            unit_price = d.get("unit_price")
            if unit_price is None:
                unit_price = variant.price if variant else Decimal("0.00")
            discount = d.get("discount_amount") or Decimal("0.00")
            line_subtotal = unit_price * d["quantity"]
            if unit_price < 0 or discount < 0 or discount > line_subtotal:
                raise ValidationError(
                    {"details": "Prices and discounts must be non-negative, and discounts can't exceed the item subtotal."}
                )
            line_total = line_subtotal - discount

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
        details = serializer.validated_data.pop("details", [])
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
        draft = DraftOrder.objects.select_for_update().get(
            pk=serializer.instance.pk, store=self.request.store
        )
        if draft.status != OPEN_DRAFT:
            raise ValidationError("Completed drafts can't be changed.")
        details = serializer.validated_data.pop("details", None)
        serializer.instance = draft
        draft = serializer.save()
        if details is not None:
            self._save_details(draft, details)

    @transaction.atomic
    def perform_destroy(self, instance):
        draft = DraftOrder.objects.select_for_update().get(
            pk=instance.pk, store=self.request.store
        )
        if draft.status != OPEN_DRAFT:
            raise ValidationError("Completed drafts can't be deleted.")
        try:
            draft.details.all().delete()
            draft.delete()
        except ProtectedError:
            raise ValidationError("This draft is still referenced by other records and can't be deleted.")

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        with transaction.atomic():
            draft = DraftOrder.objects.select_for_update().get(
                pk=self.get_object().pk, store=request.store
            )
            if draft.order_id is not None:
                return Response(
                    OrderSerializer(draft.order).data,
                    status=status.HTTP_200_OK,
                )
            if draft.status != OPEN_DRAFT:
                raise ValidationError("This draft is already completed.")
            if draft.customer.store_id != request.store.pk:
                raise ValidationError({"customer": "Customer does not belong to this store."})

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
                request.store, lines, customer=draft.customer, currency=draft.currency_code, user=request.user
            )
            draft.order = order
            draft.status = COMPLETED_DRAFT
            draft.save(update_fields=["order", "status", "updated_at"])

        return Response(OrderSerializer(order).data, status=status.HTTP_201_CREATED)
