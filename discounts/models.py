import uuid

from django.db import models

from catalog.models import Product
from orders.models import Order
from tenancy.models import Store


class Discount(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        db_column="store_id",
        related_name="discounts",
    )
    name = models.CharField(max_length=150)
    discount_type = models.CharField(max_length=30)
    value = models.DecimalField(max_digits=12, decimal_places=2)
    minimum_order_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    maximum_discount_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField(null=True, blank=True)
    usage_limit = models.IntegerField(null=True, blank=True)
    per_customer_limit = models.IntegerField(null=True, blank=True)
    is_active = models.BooleanField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_deleted = models.BooleanField()
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = '"discounts"."discounts"'
        verbose_name = "Discount"
        verbose_name_plural = "Discounts"

    def __str__(self):
        return self.name


class DiscountProduct(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    discount = models.ForeignKey(
        Discount,
        on_delete=models.PROTECT,
        db_column="discount_id",
        related_name="products",
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        db_column="product_id",
        related_name="discount_products",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_deleted = models.BooleanField()
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = '"discounts"."discount_products"'
        verbose_name = "Discount Product"
        verbose_name_plural = "Discount Products"
        constraints = [
            models.UniqueConstraint(
                fields=["discount", "product"],
                name="uq_discount_product",
            ),
        ]

    def __str__(self):
        return f"{self.discount} - {self.product}"


class DiscountCode(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    discount = models.ForeignKey(
        Discount,
        on_delete=models.PROTECT,
        db_column="discount_id",
        related_name="codes",
    )
    code = models.CharField(max_length=50)
    is_active = models.BooleanField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_deleted = models.BooleanField()
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = '"discounts"."discount_codes"'
        verbose_name = "Discount Code"
        verbose_name_plural = "Discount Codes"
        constraints = [
            models.UniqueConstraint(
                fields=["discount", "code"],
                name="uq_discount_code",
            ),
        ]

    def __str__(self):
        return self.code


class OrderDiscount(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    order = models.ForeignKey(
        Order,
        on_delete=models.PROTECT,
        db_column="order_id",
        related_name="discounts",
    )
    discount = models.ForeignKey(
        Discount,
        on_delete=models.PROTECT,
        db_column="discount_id",
        related_name="order_discounts",
    )
    discount_code = models.ForeignKey(
        DiscountCode,
        on_delete=models.PROTECT,
        db_column="discount_code_id",
        related_name="order_discounts",
        null=True,
        blank=True,
    )
    discount_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_deleted = models.BooleanField()
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = '"discounts"."order_discounts"'
        verbose_name = "Order Discount"
        verbose_name_plural = "Order Discounts"

    def __str__(self):
        return f"{self.order} - {self.discount}"