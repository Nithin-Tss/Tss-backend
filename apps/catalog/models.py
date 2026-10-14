import uuid

from django.db import models
from django.utils.text import slugify

from apps.tenancy.models import Store


class Category(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        db_column="store_id",
        related_name="categories",
    )

    name = models.CharField(max_length=255)

    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        db_column="parent_id",
        related_name="children",
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"catalog"."categories"'
        verbose_name = "Category"
        verbose_name_plural = "Categories"

    def __str__(self):
        return self.name


# Free-text columns on every product, edited on the product page and filled by
# the CSV import.
EXTRA_FIELDS = (
    "allergens",
    "food_recommendations",
    "storage_suggestions",
    "column1",
    "column2",
    "column3",
    "column4",
    "column5",
    "column6",
    "column7",
)

class Brand(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        db_column="store_id",
        related_name="brands",
    )

    name = models.CharField(max_length=255)
    description = models.TextField(null=True, blank=True)
    logo_url = models.TextField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

   
    class Meta:
        db_table = '"catalog"."brands"'
        verbose_name = "Brand"
        verbose_name_plural = "Brands"
        constraints = [
            models.UniqueConstraint(
                models.functions.Lower(
                    models.functions.Trim("name")
                ),
                "store",
                name="uq_brand_store_name_ci",
            ),
        ]


    def __str__(self):
        return self.name



class Product(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        db_column="store_id",
        related_name="products",
    )

    title = models.CharField(max_length=255)

    status = models.CharField(max_length=20)

    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        db_column="category_id",
        related_name="products",
        null=True,
        blank=True,
    )

    brand = models.ForeignKey(
        Brand,
        on_delete=models.PROTECT,
        db_column="brand_id",
        related_name="products",
        null=True,
        blank=True,
    )

    product_type = models.CharField(
        max_length=100,
        null=True,
        blank=True,
    )

    vendor = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )

    description = models.TextField(
        null=True,
        blank=True,
    )

    seo_title = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )

    seo_description = models.TextField(
        null=True,
        blank=True,
    )

    theme_template = models.CharField(
        max_length=100,
        null=True,
        blank=True,
    )

    # Created 10 more empty text fields for further use.
    food_recommendations = models.TextField(
        null=True,
        blank=True,
    )

    storage_suggestions = models.TextField(
        null=True,
        blank=True,
    )

    allergens = models.TextField(
        null=True,
        blank=True,
    )

    
    column1 = models.TextField(null=True, blank=True)
    column2 = models.TextField(null=True, blank=True)
    column3 = models.TextField(null=True, blank=True)
    column4 = models.TextField(null=True, blank=True)
    column5 = models.TextField(null=True, blank=True)
    column6 = models.TextField(null=True, blank=True)
    column7 = models.TextField(null=True, blank=True)




    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"catalog"."products"'
        verbose_name = "Product"
        verbose_name_plural = "Products"

        indexes = [
            models.Index(
                fields=["store", "-created_at"],
                name="prd_store_created_idx",
            ),
            models.Index(
                fields=["store", "status", "-created_at"],
                name="prd_store_stat_date_idx",
            ),
        ]

    def __str__(self):
        return self.title


class ProductVariant(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        db_column="product_id",
        related_name="variants",
    )

    sku = models.CharField(
        max_length=100,
        null=True,
        blank=True,
    )

    price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"catalog"."product_variants"'
        verbose_name = "Product Variant"
        verbose_name_plural = "Product Variants"

        indexes = [
            models.Index(
                fields=["product", "created_at"],
                name="pvar_prod_created_idx",
            ),
        ]

    def __str__(self):
        return self.sku or str(self.id)


class ProductImage(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        db_column="product_id",
        related_name="images",
    )

    url = models.TextField()

    position = models.IntegerField()

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = '"catalog"."product_images"'
        verbose_name = "Product Image"
        verbose_name_plural = "Product Images"

    def __str__(self):
        return self.url


def handle_base(title):
    """A URL handle from a title: "Summer Sale!" -> "summer-sale"."""
    return slugify(title or "")[:200].strip("-") or "collection"


def unique_handle(store, title, exclude_pk=None, taken=()):
    """handle_base(title), with -2, -3, ... added while the store already uses it."""
    base = handle_base(title)
    used = set(Collection.objects.filter(store=store, handle__startswith=base)
               .exclude(pk=exclude_pk).values_list("handle", flat=True)) | set(taken)
    used.update(RESERVED_HANDLES)
    handle, n = base, 2
    while handle in used:
        handle, n = f"{base}-{n}", n + 1
    return handle


# /collections/all is the all-products page
RESERVED_HANDLES = {"all"}


class Collection(models.Model):
    MANUAL = "manual"        # products picked one by one (collection_products)
    AUTOMATED = "automated"  # products matching its rules (collection_rules), kept up to date
    TYPES = (MANUAL, AUTOMATED)
    ACTIVE = "active"
    DRAFT = "draft"          # hidden from the store
    STATUSES = (ACTIVE, DRAFT)

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        db_column="store_id",
        related_name="collections",
    )

    # The collection's title
    name = models.CharField(max_length=255)
    description = models.TextField(default="", blank=True)
    image_url = models.CharField(max_length=500, default="", blank=True)
    # Its address on the store: /collections/<handle>
    handle = models.SlugField(max_length=255, blank=True, default="", allow_unicode=False)
    type = models.CharField(max_length=20, default=MANUAL)
    status = models.CharField(max_length=20, default=ACTIVE)
    # Automated: a product must match "all" rules, or "any" one of them
    match = models.CharField(max_length=10, default="all")
    # Automated: product ids kept out even when they match ("Exclude")
    excluded_products = models.JSONField(default=list, blank=True)
    # Unused; from the first version of the Add collection page
    conditions = models.JSONField(default=list, blank=True)
    theme_template = models.CharField(max_length=100, default="Default collection", blank=True)
    seo_title = models.CharField(max_length=255, default="", blank=True)
    seo_description = models.TextField(default="", blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"catalog"."collections"'
        verbose_name = "Collection"
        verbose_name_plural = "Collections"
        constraints = [
            models.UniqueConstraint(fields=["store", "handle"], name="uq_collection_store_handle"),
        ]

    def save(self, *args, **kwargs):
        # Collections made elsewhere (the product form, imports) get a handle from their name
        if not self.handle:
            self.handle = unique_handle(self.store, self.name, exclude_pk=self.pk)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class CollectionRule(models.Model):
    """One condition of an automated collection, e.g. Tag / is equal to / Summer."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, db_column="store_id", related_name="collection_rules")
    collection = models.ForeignKey(
        Collection, on_delete=models.CASCADE, db_column="collection_id", related_name="rules",
    )
    field = models.CharField(max_length=40)
    operator = models.CharField(max_length=20)
    value = models.CharField(max_length=255)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = '"catalog"."collection_rules"'
        ordering = ["position"]
        verbose_name = "Collection rule"
        verbose_name_plural = "Collection rules"

    def __str__(self):
        return f"{self.field} {self.operator} {self.value}"


class ProductCollection(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        db_column="product_id",
        related_name="product_collections",
    )

    collection = models.ForeignKey(
        Collection,
        on_delete=models.PROTECT,
        db_column="collection_id",
        related_name="product_collections",
    )

    class Meta:
        db_table = '"catalog"."product_collections"'
        verbose_name = "Product Collection"
        verbose_name_plural = "Product Collections"

        constraints = [
            models.UniqueConstraint(
                fields=["product", "collection"],
                name="uq_product_collection",
            )
        ]


class Tag(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        db_column="store_id",
        related_name="tags",
    )

    name = models.CharField(max_length=100)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = '"catalog"."tags"'
        verbose_name = "Tag"
        verbose_name_plural = "Tags"

    def __str__(self):
        return self.name


class ProductTag(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        db_column="product_id",
        related_name="product_tags",
    )

    tag = models.ForeignKey(
        Tag,
        on_delete=models.PROTECT,
        db_column="tag_id",
        related_name="product_tags",
    )

    class Meta:
        db_table = '"catalog"."product_tags"'
        verbose_name = "Product Tag"
        verbose_name_plural = "Product Tags"

        constraints = [
            models.UniqueConstraint(
                fields=["product", "tag"],
                name="uq_product_tag",
            )
        ]
class SalesChannel(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        db_column="store_id",
        related_name="sales_channels",
    )

    name = models.CharField(max_length=100)

    type = models.CharField(max_length=50)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = '"catalog"."sales_channels"'
        verbose_name = "Sales Channel"
        verbose_name_plural = "Sales Channels"

    def __str__(self):
        return self.name


class ProductSalesChannel(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        db_column="product_id",
        related_name="product_sales_channels",
    )

    sales_channel = models.ForeignKey(
        SalesChannel,
        on_delete=models.PROTECT,
        db_column="sales_channel_id",
        related_name="product_sales_channels",
    )

    class Meta:
        db_table = '"catalog"."product_sales_channels"'
        verbose_name = "Product Sales Channel"
        verbose_name_plural = "Product Sales Channels"

        constraints = [
            models.UniqueConstraint(
                fields=["product", "sales_channel"],
                name="uq_product_sales_channel",
            )
        ]


class CollectionSalesChannel(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    collection = models.ForeignKey(
        Collection,
        on_delete=models.CASCADE,
        db_column="collection_id",
        related_name="collection_sales_channels",
    )

    sales_channel = models.ForeignKey(
        SalesChannel,
        on_delete=models.CASCADE,
        db_column="sales_channel_id",
        related_name="collection_sales_channels",
    )

    class Meta:
        db_table = '"catalog"."collection_sales_channels"'
        verbose_name = "Collection Sales Channel"
        verbose_name_plural = "Collection Sales Channels"

        constraints = [
            models.UniqueConstraint(
                fields=["collection", "sales_channel"],
                name="uq_collection_sales_channel",
            )
        ]

    def __str__(self):
        return f"{self.collection.name} - {self.sales_channel.name}"


class ProductReview(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        db_column="store_id",
        related_name="product_reviews",
    )

    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        db_column="product_id",
        related_name="reviews",
    )

    customer = models.ForeignKey(
        "customers.Customer",
        on_delete=models.PROTECT,
        db_column="customer_id",
        related_name="product_reviews",
    )

    order_item = models.ForeignKey(
       "orders.OrderItem",
        on_delete=models.PROTECT,
         db_column="order_item_id",
        related_name="product_reviews",
        null=True,
        blank=True,
    )

    rating = models.SmallIntegerField()

    review_body = models.TextField(
        null=True,
        blank=True,
    )

    status = models.CharField(max_length=20)

    is_verified_purchase = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"catalog"."product_reviews"'
        verbose_name = "Product Review"
        verbose_name_plural = "Product Reviews"

        constraints = [
            models.CheckConstraint(
                condition=models.Q(rating__gte=1, rating__lte=5),
                name="ck_product_review_rating",
            )
        ]

    def __str__(self):
        return f"{self.product} - {self.rating}/5"