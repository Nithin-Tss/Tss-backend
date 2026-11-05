import uuid



from django.db import transaction
from django.db.models import CharField, Q
from django.db.models.functions import Cast
from django.utils import timezone

from rest_framework import mixins, status

from rest_framework.decorators import action

from rest_framework.exceptions import ValidationError

from rest_framework.parsers import MultiPartParser

from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle



from apps.audit.services import audited_update
from apps.core.viewsets import StoreScopedGenericViewSet, StoreScopedViewSet



from .models import Catalogue, Category, Collection, Product, ProductVariant

from .serializers import (

    CatalogueSerializer,
    CategorySerializer,

    CollectionRuleSerializer,

    CollectionSerializer,

    ProductInputSerializer,

    ProductSerializer,

    SalesChannelSerializer,

    VariantSerializer,

)

from .services import (

    STATUSES as PRODUCT_STATUSES,

    ProductInUse,

    create_variant,

    delete_product,
    forget_included_collection,
    delete_all_products,
    delete_products,

    delete_variant,

    ACTIVE,
    NO_PRICE_MESSAGE,
    import_products,
    included_product_sources,

    matching_products,

    sales_channels_for,

    save_product,

    update_variant,

)



# Product photos: JPG, PNG or WEBP up to 1MB, checked and cleaned in apps/media/images.py
# (MAX_IMAGE_SIZE and image_extension are importable from here as before)
from apps.media.images import MAX_IMAGE_SIZE, clean_image, image_extension, public_image_url  # noqa: E402,F401

MAX_BULK_ROWS = 500


def _products_by_filter(store, spec):
    """
    The store's products that the admin list's filter shows: {"status": "all"|<status>, "q": "text"}.
    q matches the title, a SKU, an id or the category name, like the list's search box.
    """
    if not isinstance(spec, dict):
        raise ValidationError({"filter": "Send the list's filter as an object."})
    status = spec.get("status") or "all"
    query = str(spec.get("q") or "").strip()
    if status != "all" and status not in PRODUCT_STATUSES:
        raise ValidationError({"filter": "Status must be all, active, draft or archived."})
    qs = Product.objects.filter(store=store)
    if status != "all":
        qs = qs.filter(status=status)
    if query:
        qs = qs.annotate(_id_text=Cast("id", CharField()), _variant_id_text=Cast("variants__id", CharField())).filter(
            Q(title__icontains=query) | Q(variants__sku__icontains=query) | Q(_id_text__icontains=query)
            | Q(_variant_id_text__icontains=query) | Q(category__name__icontains=query)
        )
    return Product.objects.filter(store=store, pk__in=qs.values("pk"))





class ProductViewSet(StoreScopedViewSet):

    """

    /api/v1/catalog/products/        list, create

    /api/v1/catalog/products/<id>/   retrieve, update (PUT/PATCH), delete

    """



    permission_resource = "products"
    action_permissions = {"bulk_delete": "products.delete", "delete_all": "products.delete"}
    queryset = Product.objects.select_related("category").prefetch_related(

        "variants__inventory_items",

        "images",

        "product_collections__collection",

        "product_tags__tag",

        "product_sales_channels",

    ).order_by("-created_at")

    serializer_class = ProductSerializer



    def _save(self, request, product=None, partial=False):

        context = self.get_serializer_context()
        if product is not None:
            context["kept_images"] = set(product.images.values_list("url", flat=True))
        data = ProductInputSerializer(data=request.data, partial=partial, context=context)

        data.is_valid(raise_exception=True)



        try:

            product = save_product(request.store, data.validated_data, product, user=request.user)

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
        -> {"created": n, "skipped": n, "stockUpdated": n, "errors": [{"index": i, "message": "..."}]}
        Used by the admin "Import products" popup. Invalid rows are reported, not fatal.
        """
        rows = request.data.get("products") if isinstance(request.data, dict) else None
        if not isinstance(rows, list) or not rows:
            raise ValidationError({"products": "Send a non-empty list of products."})
        if len(rows) > MAX_BULK_ROWS:
            raise ValidationError({"products": f"Send at most {MAX_BULK_ROWS} products per request."})

        # A SKU that is already in the store only updates stock: its price is not read
        known_skus = set(
            ProductVariant.objects.filter(
                product__store=request.store,
                sku__in={str(r.get("sku") or "").strip() for r in rows if isinstance(r, dict)} - {""},
            ).values_list("sku", flat=True)
        )
        valid, errors = [], []
        for index, row in enumerate(rows):
            serializer = ProductInputSerializer(
                data=row, context={**self.get_serializer_context(), "location_optional": True, "external_images": True}
            )
            if serializer.is_valid():
                if not (serializer.validated_data.get("sku") or "").strip():
                    errors.append({"index": index, "message": "sku: SKU is required."})
                    continue
                if (
                    serializer.validated_data.get("status") == ACTIVE
                    and serializer.validated_data.get("price") is None
                    and serializer.validated_data["sku"].strip() not in known_skus
                ):
                    errors.append({"index": index, "message": f"price: {NO_PRICE_MESSAGE}"})
                    continue
                valid.append(serializer.validated_data)
                continue
            parts = []
            for field, messages in serializer.errors.items():
                message = messages[0] if isinstance(messages, list) else str(messages)
                parts.append(f"{field}: {message}")
            errors.append({"index": index, "message": "; ".join(parts)})

        created, skipped, stock_updated = (
            import_products(request.store, valid, user=request.user) if valid else (0, 0, 0)
        )
        return Response(
            {"created": created, "skipped": skipped, "stockUpdated": stock_updated, "errors": errors}
        )

    @action(detail=False, methods=["post"], url_path="bulk-status")
    def bulk_status(self, request):
        """
        POST /api/v1/catalog/products/bulk-status/  {"ids": [id, ...], "status": "active"|"draft"|"archived"}  (up to 500)
        or {"filter": {"status": "all", "q": ""}, "status": ...} for every product the list shows
        -> {"updated": n}
        """
        data = request.data if isinstance(request.data, dict) else {}
        new_status = data.get("status")
        if new_status not in PRODUCT_STATUSES:
            raise ValidationError({"status": "Status must be active, draft or archived."})
        if data.get("filter") is not None:
            # "Select all N products": every product the list's filter matches, found here, not sent as ids
            targets = _products_by_filter(request.store, data["filter"])
        else:
            ids = data.get("ids")
            if not isinstance(ids, list) or not ids:
                raise ValidationError({"ids": "Send a non-empty list of product ids."})
            if len(ids) > MAX_BULK_ROWS:
                raise ValidationError({"ids": f"Send at most {MAX_BULK_ROWS} ids per request."})
            try:
                ids = [uuid.UUID(str(i)) for i in ids]
            except ValueError:
                raise ValidationError({"ids": "Every id must be a valid product id."})
            targets = Product.objects.filter(store=request.store, pk__in=ids)

        if new_status == ACTIVE:
            # Same rule as the product form: no option may be left without a price when a product goes on sale
            unpriced = (
                targets.filter(variants__price=0).exclude(status=ACTIVE)
                .distinct().count()
            )
            if unpriced:
                raise ValidationError({"status": (
                    f"{unpriced} of these products {'has' if unpriced == 1 else 'have'} an option priced 0.00. "
                    "Enter a price for every option before making them active."
                )})

        updated = audited_update(targets, status=new_status, updated_at=timezone.now())
        return Response({"updated": updated})

    @action(detail=False, methods=["post"], url_path="bulk-delete")
    def bulk_delete(self, request):
        """
        POST /api/v1/catalog/products/bulk-delete/  {"ids": [id, ...]}  (up to 500)
        or {"filter": {"status": "all", "q": ""}} for every product the list shows
        -> {"deleted": n, "blocked": ["title", ...]}
        Products used by orders or inventory are kept and listed in "blocked".
        """
        data = request.data if isinstance(request.data, dict) else {}
        if data.get("filter") is not None:
            ids = list(_products_by_filter(request.store, data["filter"]).values_list("pk", flat=True))
        else:
            ids = data.get("ids")
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

    @action(detail=False, methods=["post"], url_path="delete-all")
    def delete_all(self, request):
        """
        POST /api/v1/catalog/products/delete-all/  {"confirm": true}
        -> {"deleted": n, "blocked": ["title", ...]}
        Deletes every product in the store, active ones included, along with their stock history.
        """
        if not (isinstance(request.data, dict) and request.data.get("confirm") is True):
            raise ValidationError({"confirm": "Send confirm: true to delete all products."})
        deleted, blocked = delete_all_products(request.store)
        return Response({"deleted": deleted, "blocked": blocked})

    def get_throttles(self):
        # Only photo uploads are rate limited (see DEFAULT_THROTTLE_RATES["product_image_upload"])
        if self.action == "upload_image":
            self.throttle_scope = "product_image_upload"
            return [ScopedRateThrottle()]
        return super().get_throttles()

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

            raise ValidationError({"image": "Image is too large. Maximum size is 1MB per image."})



        data, _, ctype = clean_image(file.read())

        from apps.media.services import check_quota
        check_quota(request.store.store_id, len(data))

        # Stored in the database (the server's disk is wiped on deploy). Served by the
        # public /s/_img/<id> route, the same one customizer pictures use.
        from apps.themes.models import ThemeImage

        image = ThemeImage.objects.create(
            store_id=request.store.store_id, content_type=ctype, data=data, size=len(data),
        )
        url = public_image_url(request, f"/s/_img/{image.id}")

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

    """
    /api/v1/catalog/collections/           list, create
    /api/v1/catalog/collections/<id>/      retrieve, update, delete
    /api/v1/catalog/collections/preview/   POST {rules, match, excluded_products, include_collections,
                                           type, products} -> products an automated collection with these (unsaved)
                                           rules would hold ("products"), and the ones that come from the ticked
                                           collections, nested too, not already its own ("included", each with
                                           "from_collection"; "included_count" is the full total)
    """

    permission_resource = "products"
    action_permissions = {"preview": "products.view"}
    queryset = Collection.objects.order_by("name")
    serializer_class = CollectionSerializer

    # Most products the preview returns
    PREVIEW_LIMIT = 250

    def get_queryset(self):
        from django.db.models import Count
        return (
            super()
            .get_queryset()
            .annotate(
                products_count_annotated=Count("product_collections", distinct=True),
                sales_channels_count_annotated=Count(
                    "product_collections__product__product_sales_channels__sales_channel",
                    distinct=True,
                ),
            )
            .prefetch_related(
                "rules",
                "product_collections__product__product_sales_channels__sales_channel",
            )
        )

    def perform_destroy(self, instance):
        # Its product links and rules go with it; the products themselves stay. Collections that
        # ticked it simply lose it from their list.
        with transaction.atomic():
            forget_included_collection(instance)
            instance.product_collections.all().delete()
            super().perform_destroy(instance)

    @action(detail=False, methods=["post"])
    def preview(self, request):
        if not isinstance(request.data, dict):
            raise ValidationError({"detail": "Send a JSON object."})
        rules = CollectionRuleSerializer(data=request.data.get("rules") or [], many=True)
        rules.is_valid(raise_exception=True)
        match = "any" if request.data.get("match") == "any" else "all"
        excluded = self._ids(request.data.get("excluded_products"), "excluded_products", "product")
        manual = request.data.get("type") == Collection.MANUAL

        if manual:  # the products picked by hand are the collection's own; the page already has them
            own = self._ids(request.data.get("products"), "products", "product")
            products = ProductViewSet.queryset.none()
        else:
            matches = matching_products(request.store, rules.validated_data, match, excluded)
            own = [str(pk) for pk in matches.values_list("pk", flat=True)]
            products = ProductViewSet.queryset.filter(pk__in=matches.values("pk"))

        # Products coming from the collections ticked on this (maybe unsaved) collection, nested ones too
        ticked = self._ids(request.data.get("include_collections"), "include_collections", "collection")
        sources, names = included_product_sources(request.store, ticked, exclude=own)
        included = list(ProductViewSet.queryset.filter(pk__in=list(sources)[:self.PREVIEW_LIMIT]))
        order = {pk: i for i, pk in enumerate(sources)}
        included.sort(key=lambda p: order[str(p.pk)])
        included_data = ProductSerializer(included, many=True).data
        for row, product in zip(included_data, included):
            source = sources[str(product.pk)]
            row["from_collection"] = {"id": source, "name": names[source]}

        return Response({
            "count": products.count(),
            "products": ProductSerializer(products[:self.PREVIEW_LIMIT], many=True).data,
            "included_count": len(sources),
            "included": included_data,
        })

    @staticmethod
    def _ids(value, field, what):
        """A list of ids from the request, as strings."""
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValidationError({field: f"Send a list of {what} ids."})
        try:
            return [str(uuid.UUID(str(pk))) for pk in value]
        except ValueError:
            raise ValidationError({field: f"Every id must be a valid {what} id."})





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



class CatalogueViewSet(StoreScopedViewSet):
    """/api/v1/catalog/catalogues/  list, create, retrieve, update, delete"""

    permission_resource = "products"
    queryset = Catalogue.objects.order_by("name")
    serializer_class = CatalogueSerializer
