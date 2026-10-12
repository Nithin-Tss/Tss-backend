import uuid



from django.conf import settings

from django.core.files.storage import default_storage

from rest_framework import mixins, status

from rest_framework.decorators import action

from rest_framework.exceptions import ValidationError

from rest_framework.parsers import MultiPartParser

from rest_framework.response import Response



from apps.core.viewsets import StoreScopedGenericViewSet, StoreScopedViewSet



from .models import Category, Collection, Product, ProductVariant

from .serializers import (

    CategorySerializer,

    CollectionSerializer,

    ProductInputSerializer,

    ProductSerializer,

    SalesChannelSerializer,

    VariantSerializer,

)

from .services import (

    ProductInUse,

    create_variant,

    delete_product,
    delete_products,

    delete_variant,

    import_products,

    sales_channels_for,

    save_product,

    update_variant,

)



# Product photos: JPG, PNG or WEBP up to 5MB, checked by their first bytes, not the file name

MAX_IMAGE_SIZE = 5 * 1024 * 1024

MAX_BULK_ROWS = 500

IMAGE_SIGNATURES = {"jpg": (b"\xff\xd8\xff",), "png": (b"\x89PNG\r\n\x1a\n",)}





def image_extension(head):

    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":

        return "webp"

    for ext, signatures in IMAGE_SIGNATURES.items():

        if head.startswith(signatures):

            return ext

    return None





class ProductViewSet(StoreScopedViewSet):

    """

    /api/v1/catalog/products/        list, create

    /api/v1/catalog/products/<id>/   retrieve, update (PUT/PATCH), delete

    """



    permission_resource = "products"
    action_permissions = {"bulk_delete": "products.delete"}
    queryset = Product.objects.select_related("category").prefetch_related(

        "variants",

        "images",

        "product_collections__collection",

        "product_tags__tag",

        "product_sales_channels",

    ).order_by("-created_at")

    serializer_class = ProductSerializer



    def _save(self, request, product=None, partial=False):

        data = ProductInputSerializer(data=request.data, partial=partial, context=self.get_serializer_context())

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



    @action(detail=False, methods=["post"], url_path="bulk")
    def bulk(self, request):
        """
        POST /api/v1/catalog/products/bulk/  {"products": [row, ...]}  (up to 500 rows)
        -> {"created": n, "skipped": n, "errors": [{"index": i, "message": "..."}]}
        Used by the admin "Import products" popup. Invalid rows are reported, not fatal.
        """
        rows = request.data.get("products") if isinstance(request.data, dict) else None
        if not isinstance(rows, list) or not rows:
            raise ValidationError({"products": "Send a non-empty list of products."})
        if len(rows) > MAX_BULK_ROWS:
            raise ValidationError({"products": f"Send at most {MAX_BULK_ROWS} products per request."})

        valid, errors = [], []
        for index, row in enumerate(rows):
            serializer = ProductInputSerializer(
                data=row, context={**self.get_serializer_context(), "location_optional": True}
            )
            if serializer.is_valid():
                if not (serializer.validated_data.get("sku") or "").strip():
                    errors.append({"index": index, "message": "sku: SKU is required."})
                    continue
                valid.append(serializer.validated_data)
                continue
            parts = []
            for field, messages in serializer.errors.items():
                message = messages[0] if isinstance(messages, list) else str(messages)
                parts.append(f"{field}: {message}")
            errors.append({"index": index, "message": "; ".join(parts)})

        created, skipped = import_products(request.store, valid) if valid else (0, 0)
        return Response({"created": created, "skipped": skipped, "errors": errors})

    @action(detail=False, methods=["post"], url_path="bulk-delete")
    def bulk_delete(self, request):
        """
        POST /api/v1/catalog/products/bulk-delete/  {"ids": [id, ...]}  (up to 500)
        -> {"deleted": n, "blocked": ["title", ...]}
        Products used by orders or inventory are kept and listed in "blocked".
        """
        ids = request.data.get("ids") if isinstance(request.data, dict) else None
        if not isinstance(ids, list) or not ids:
            raise ValidationError({"ids": "Send a non-empty list of product ids."})
        if len(ids) > MAX_BULK_ROWS:
            raise ValidationError({"ids": f"Send at most {MAX_BULK_ROWS} ids per request."})
        try:
            ids = [uuid.UUID(str(i)) for i in ids]
        except ValueError:
            raise ValidationError({"ids": "Every id must be a valid product id."})

        deleted, blocked = delete_products(request.store, ids)
        return Response({"deleted": deleted, "blocked": blocked})

    @action(detail=False, methods=["post"], url_path="images", parser_classes=[MultiPartParser])

    def upload_image(self, request):

        """

        POST /api/v1/catalog/products/images/  (multipart, field "image") -> {"url": ...}

        Upload a photo first, then send its url in the product's "images" list.

        """

        file = request.FILES.get("image")

        if file is None:

            raise ValidationError({"image": "Choose an image to upload."})

        if file.size > MAX_IMAGE_SIZE:

            raise ValidationError({"image": "Image is too large. Maximum size is 5MB."})



        ext = image_extension(file.read(12))

        if ext is None:

            raise ValidationError({"image": "Not a supported image. Use JPG, PNG or WEBP."})

        file.seek(0)



        name = default_storage.save(f"products/{request.store.store_id}/{uuid.uuid4().hex}.{ext}", file)

        url = request.build_absolute_uri(settings.MEDIA_URL + name)

        return Response({"url": url}, status=status.HTTP_201_CREATED)





class VariantViewSet(StoreScopedViewSet):

    """

    /api/v1/catalog/variants/  ?product=<id>   list, create

    /api/v1/catalog/variants/<id>/             retrieve, update, delete

    """



    permission_resource = "products"
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

    permission_resource = "products"
    queryset = Collection.objects.order_by("name")

    serializer_class = CollectionSerializer





class CategoryViewSet(StoreScopedViewSet):

    """

    /api/v1/catalog/categories/  the store's categories; `parent` makes one a subcategory

    """



    permission_resource = "products"
    queryset = Category.objects.order_by("name")

    serializer_class = CategorySerializer





class SalesChannelViewSet(mixins.ListModelMixin, StoreScopedGenericViewSet):

    """/api/v1/catalog/sales-channels/  where the store sells (Online Store, Point of Sale, ...)"""



    permission_resource = "products"
    serializer_class = SalesChannelSerializer



    def get_queryset(self):

        return sales_channels_for(self.request.store)

