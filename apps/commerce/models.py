import uuid

from django.db import models

from apps.catalog.models import Product, ProductVariant
from apps.customers.models import Customer
from apps.tenancy.models import Store


class Cart(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        db_column="store_id",
        related_name="carts",
    )

    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        db_column="customer_id",
        related_name="carts",
        null=True,
        blank=True,
    )

    session_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )

    currency = models.CharField(
        max_length=3,
    )

    status = models.CharField(
        max_length=30,
    )

    metadata = models.JSONField(
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    is_deleted = models.BooleanField()

    deleted_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        db_table = '"commerce"."carts"'
        verbose_name = "Cart"
        verbose_name_plural = "Carts"

    def __str__(self):
        return str(self.id)


class CartItem(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    cart = models.ForeignKey(
        Cart,
        on_delete=models.PROTECT,
        db_column="cart_id",
        related_name="items",
    )

    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        db_column="product_id",
        related_name="cart_items",
    )

    variant = models.ForeignKey(
        ProductVariant,
        on_delete=models.PROTECT,
        db_column="variant_id",
        related_name="cart_items",
        null=True,
        blank=True,
    )

    quantity = models.IntegerField()

    unit_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )

    metadata = models.JSONField(
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    is_deleted = models.BooleanField()

    deleted_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        db_table = '"commerce"."cart_items"'
        verbose_name = "Cart Item"
        verbose_name_plural = "Cart Items"

        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0),
                name="ck_cart_item_quantity_positive",
            )
        ]

    def __str__(self):
        return f"{self.cart} - {self.product}"


class Checkout(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        db_column="store_id",
        related_name="checkouts",
    )

    cart = models.ForeignKey(
        Cart,
        on_delete=models.PROTECT,
        db_column="cart_id",
        related_name="checkouts",
    )

    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        db_column="customer_id",
        related_name="checkouts",
        null=True,
        blank=True,
    )

    currency = models.CharField(
        max_length=3,
    )

    subtotal = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )

    discount_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )

    tax_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )

    shipping_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )

    total_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )

    status = models.CharField(
        max_length=30,
    )

    shipping_address = models.JSONField(
        null=True,
        blank=True,
    )

    billing_address = models.JSONField(
        null=True,
        blank=True,
    )

    metadata = models.JSONField(
        null=True,
        blank=True,
    )

    email = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )

    abandoned_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    recovery_status = models.CharField(
        max_length=30,
        null=True,
        blank=True,
    )

    recovered_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    is_deleted = models.BooleanField()

    deleted_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        db_table = '"commerce"."checkouts"'
        verbose_name = "Checkout"
        verbose_name_plural = "Checkouts"

    def __str__(self):
        return str(self.id)


class CheckoutItem(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    checkout = models.ForeignKey(
        Checkout,
        on_delete=models.PROTECT,
        db_column="checkout_id",
        related_name="items",
    )

    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        db_column="product_id",
        related_name="checkout_items",
    )

    variant = models.ForeignKey(
        ProductVariant,
        on_delete=models.PROTECT,
        db_column="variant_id",
        related_name="checkout_items",
        null=True,
        blank=True,
    )

    product_name = models.CharField(
        max_length=255,
    )

    sku = models.CharField(
        max_length=100,
        null=True,
        blank=True,
    )

    quantity = models.IntegerField()

    unit_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )

    total_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )

    metadata = models.JSONField(
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    is_deleted = models.BooleanField()

    deleted_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        db_table = '"commerce"."checkout_items"'
        verbose_name = "Checkout Item"
        verbose_name_plural = "Checkout Items"

        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0),
                name="ck_checkout_item_quantity_positive",
            )
        ]

    def __str__(self):
        return f"{self.checkout} - {self.product_name}"