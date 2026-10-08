from django.db import transaction
from django.utils import timezone
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.viewsets import StoreScopedViewSet

from . import services
from .models import Discount, DiscountCode, DiscountProduct
from .serializers import CodeCheckSerializer, DiscountSerializer


class DiscountViewSet(StoreScopedViewSet):
    """
    /api/v1/discounts/                 list, create, retrieve, update, delete
    POST /api/v1/discounts/check/      {code, subtotal} -> how much the code takes off
    """

    queryset = Discount.objects.order_by("-created_at")
    serializer_class = DiscountSerializer

    def _save_links(self, discount, codes, products):
        if codes is not None:
            keep = set(codes)
            existing = {c.code: c for c in discount.codes.all()}
            for code, row in existing.items():
                gone = code not in keep
                if row.is_deleted != gone:
                    row.is_deleted, row.deleted_at = gone, timezone.now() if gone else None
                    row.save(update_fields=["is_deleted", "deleted_at", "updated_at"])
            for code in keep - set(existing):
                DiscountCode.objects.create(discount=discount, code=code, is_active=True, is_deleted=False)

        if products is not None:
            keep = {p.pk for p in products}
            existing = {row.product_id: row for row in discount.products.all()}
            for product_id, row in existing.items():
                gone = product_id not in keep
                if row.is_deleted != gone:
                    row.is_deleted, row.deleted_at = gone, timezone.now() if gone else None
                    row.save(update_fields=["is_deleted", "deleted_at", "updated_at"])
            for product_id in keep - set(existing):
                DiscountProduct.objects.create(discount=discount, product_id=product_id, is_deleted=False)

    @transaction.atomic
    def perform_create(self, serializer):
        codes = serializer.validated_data.pop("codes", [])
        products = serializer.validated_data.pop("products", [])
        if "starts_at" not in serializer.validated_data:
            serializer.validated_data["starts_at"] = timezone.now()
        discount = serializer.save(store=self.request.store, is_deleted=False)
        self._save_links(discount, codes, products)

    @transaction.atomic
    def perform_update(self, serializer):
        codes = serializer.validated_data.pop("codes", None)
        products = serializer.validated_data.pop("products", None)
        discount = serializer.save()
        self._save_links(discount, codes, products)

    @action(detail=False, methods=["post"])
    def check(self, request):
        data = CodeCheckSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        code = services.find_code(request.store, data.validated_data["code"])
        amount = services.amount_for(code.discount, data.validated_data["subtotal"])
        return Response({
            "code": code.code,
            "discount": str(code.discount.id),
            "name": code.discount.name,
            "amount": str(amount),
        })
