from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.core.viewsets import StoreScopedViewSet

from .models import Collection, Product, ProductVariant
from .serializers import (
    CollectionSerializer,
    ProductInputSerializer,
    ProductSerializer,
    VariantSerializer,
)
from .services import (
    ProductInUse,
    create_variant,
    delete_product,
    delete_variant,
    save_product,
    update_variant,
)


class ProductViewSet(StoreScopedViewSet):
    """
    /api/v1/catalog/products/        list, create
    /api/v1/catalog/products/<id>/   retrieve, update (PUT/PATCH), delete
    """

    queryset = Product.objects.select_related("category").prefetch_related(
        "variants",
        "images",
        "product_collections__collection",
        "product_tags__tag",
    ).order_by("-created_at")
    serializer_class = ProductSerializer

    def _save(self, request, product=None, partial=False):
        data = ProductInputSerializer(data=request.data, partial=partial)
        data.is_valid(raise_exception=True)

        try:
            product = save_product(request.store, data.validated_data, product)
        except ProductInUse as e:
            raise ValidationError({"variants": str(e)})

        return self.get_queryset().get(pk=product.pk)

    def create(self, request, *args, **kwargs):
        product = self._save(request)
        return Response(ProductSerializer(product).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        product = self._save(request, self.get_object(), partial=kwargs.get("partial", False))
        return Response(ProductSerializer(product).data)

    def perform_destroy(self, instance):
        try:
            delete_product(instance)
        except ProductInUse as e:
            raise ValidationError(str(e))


class VariantViewSet(StoreScopedViewSet):
    """
    /api/v1/catalog/variants/  ?product=<id>   list, create
    /api/v1/catalog/variants/<id>/             retrieve, update, delete
    """

    queryset = ProductVariant.objects.select_related("product").order_by("created_at")
    serializer_class = VariantSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get("product"):
            qs = qs.filter(product=self.request.query_params["product"])
        return qs

    def perform_create(self, serializer):
        data = serializer.validated_data
        serializer.instance = create_variant(data["product"], data)

    def perform_update(self, serializer):
        serializer.instance = update_variant(serializer.instance, serializer.validated_data)

    def perform_destroy(self, instance):
        try:
            delete_variant(instance)
        except ProductInUse as e:
            raise ValidationError(str(e))


class CollectionViewSet(StoreScopedViewSet):
    queryset = Collection.objects.order_by("name")
    serializer_class = CollectionSerializer
