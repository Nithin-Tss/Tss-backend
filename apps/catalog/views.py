from django.db import transaction
from django.db.models import ProtectedError
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.core.viewsets import StoreScopedViewSet

from .models import Collection, Product
from .serializers import CollectionSerializer, ProductInputSerializer, ProductSerializer
from .services import save_product


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
        except ProtectedError:
            raise ValidationError(
                {"variants": "A variant that is used by orders or inventory can't be removed."}
            )

        return self.get_queryset().get(pk=product.pk)

    def create(self, request, *args, **kwargs):
        product = self._save(request)
        return Response(ProductSerializer(product).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        product = self._save(request, self.get_object(), partial=kwargs.get("partial", False))
        return Response(ProductSerializer(product).data)

    def perform_destroy(self, instance):
        try:
            with transaction.atomic():
                self._delete(instance)
        except ProtectedError:
            raise ValidationError(
                "This product is used by orders or inventory. Set it to Draft instead."
            )

    def _delete(self, instance):
        instance.product_collections.all().delete()
        instance.product_tags.all().delete()
        instance.images.all().delete()
        instance.variants.all().delete()
        instance.delete()


class CollectionViewSet(StoreScopedViewSet):
    queryset = Collection.objects.order_by("name")
    serializer_class = CollectionSerializer
