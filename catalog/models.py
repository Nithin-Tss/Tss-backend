import uuid

from django.db import models

from tenancy.models import Store


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