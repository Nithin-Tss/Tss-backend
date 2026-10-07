import uuid

from django.db import models

from catalog.models import ProductVariant
from tenancy.models import Store


class PurchaseOrder(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        db_column="store_id",
        related_name="purchase_orders",
    )

    po_number = models.CharField(
        max_length=50,
    )

    supplier_name = models.CharField(
        max_length=255,
    )

    status = models.CharField(
        max_length=30,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        db_table = '"purchasing"."purchase_orders"'
        verbose_name = "Purchase Order"
        verbose_name_plural = "Purchase Orders"

    def __str__(self):
        return self.po_number


class PurchaseOrderItem(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.PROTECT,
        db_column="purchase_order_id",
        related_name="items",
    )

    variant = models.ForeignKey(
        ProductVariant,
        on_delete=models.PROTECT,
        db_column="variant_id",
        related_name="purchase_order_items",
    )

    quantity = models.IntegerField()

    class Meta:
        db_table = '"purchasing"."purchase_order_items"'
        verbose_name = "Purchase Order Item"
        verbose_name_plural = "Purchase Order Items"

        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0),
                name="ck_purchase_order_item_quantity_positive",
            )
        ]

    def __str__(self):
        return f"{self.purchase_order} - {self.variant}"