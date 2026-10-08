import uuid

from django.db import models

from apps.catalog.models import Product, ProductVariant, SalesChannel
from apps.customers.models import Customer
from apps.identity.models import User
from apps.tenancy.models import Store


class Order(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        db_column="store_id",
        related_name="orders",
    )
    order_number = models.CharField(max_length=50)
    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        db_column="customer_id",
        related_name="orders",
        null=True,
        blank=True,
    )
    sales_channel = models.ForeignKey(
        SalesChannel,
        on_delete=models.PROTECT,
        db_column="sales_channel_id",
        related_name="orders",
        null=True,
        blank=True,
    )
    currency_code = models.CharField(max_length=3)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2)
    shipping_amount = models.DecimalField(max_digits=12, decimal_places=2)
    total = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=30)
    fulfillment_status = models.CharField(max_length=30)
    delivery_status = models.CharField(
        max_length=30,
        null=True,
        blank=True,
    )
    delivery_method = models.CharField(
        max_length=50,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"orders"."orders"'
        verbose_name = "Order"
        verbose_name_plural = "Orders"

    def __str__(self):
        return self.order_number


class OrderItem(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    order = models.ForeignKey(
        Order,
        on_delete=models.PROTECT,
        db_column="order_id",
        related_name="items",
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        db_column="product_id",
        related_name="order_items",
    )
    variant = models.ForeignKey(
        ProductVariant,
        on_delete=models.PROTECT,
        db_column="variant_id",
        related_name="order_items",
        null=True,
        blank=True,
    )
    product_name = models.CharField(max_length=255)
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
    discount_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )
    tax_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )
    total = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_deleted = models.BooleanField()
    deleted_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        db_table = '"orders"."order_items"'
        verbose_name = "Order Item"
        verbose_name_plural = "Order Items"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0),
                name="ck_order_item_quantity_positive",
            ),
        ]

    def __str__(self):
        return f"{self.order} - {self.product_name}"


class DraftOrder(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        db_column="store_id",
        related_name="draft_orders",
    )
    draft_number = models.CharField(max_length=50)
    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        db_column="customer_id",
        related_name="draft_orders",
    )
    po_number = models.CharField(
        max_length=100,
        null=True,
        blank=True,
    )
    status = models.CharField(max_length=30)
    total = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )
    currency_code = models.CharField(max_length=3)

    # Optional link to the normal order created after conversion.
    order = models.ForeignKey(
        Order,
        on_delete=models.PROTECT,
        db_column="order_id",
        related_name="source_draft",
        null=True,
        blank=True,
    )

    created_by_user = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        db_column="created_by_user_id",
        related_name="draft_orders_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"orders"."draft_orders"'
        verbose_name = "Draft Order"
        verbose_name_plural = "Draft Orders"
        constraints = [
            models.UniqueConstraint(
                fields=["order"],
                name="uq_draft_order_order",
            ),
        ]

    def __str__(self):
        return self.draft_number


class DraftOrderDetail(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    draft_order = models.ForeignKey(
        DraftOrder,
        on_delete=models.PROTECT,
        db_column="draft_order_id",
        related_name="details",
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        db_column="product_id",
        related_name="draft_order_details",
        null=True,
        blank=True,
    )
    variant = models.ForeignKey(
        ProductVariant,
        on_delete=models.PROTECT,
        db_column="variant_id",
        related_name="draft_order_details",
        null=True,
        blank=True,
    )
    title = models.CharField(max_length=255)
    quantity = models.IntegerField()
    unit_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )
    discount_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    total = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"orders"."draft_order_details"'
        verbose_name = "Draft Order Detail"
        verbose_name_plural = "Draft Order Details"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0),
                name="ck_draft_order_detail_quantity_positive",
            ),
        ]

    def __str__(self):
        return f"{self.draft_order} - {self.title}"