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

    delete_variant,

    sales_channels_for,

    save_product,

    update_variant,

)



# Product photos: JPG, PNG or WEBP up to 5MB, checked by their first bytes, not the file name

MAX_IMAGE_SIZE = 5 * 1024 * 1024

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





class CategoryViewSet(StoreScopedViewSet):

    """

    /api/v1/catalog/categories/  the store's categories; `parent` makes one a subcategory

    """



    queryset = Category.objects.order_by("name")

    serializer_class = CategorySerializer





class SalesChannelViewSet(mixins.ListModelMixin, StoreScopedGenericViewSet):

    """/api/v1/catalog/sales-channels/  where the store sells (Online Store, Point of Sale, ...)"""



    serializer_class = SalesChannelSerializer



    def get_queryset(self):

        return sales_channels_for(self.request.store)

