import uuid

from django.db import models

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

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"catalog"."products"'
        verbose_name = "Product"
        verbose_name_plural = "Products"

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


class Collection(models.Model):
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

    name = models.CharField(max_length=255)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"catalog"."collections"'
        verbose_name = "Collection"
        verbose_name_plural = "Collections"

    def __str__(self):
        return self.name


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