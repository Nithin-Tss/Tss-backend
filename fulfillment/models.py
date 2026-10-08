import uuid

from django.db import models

from orders.models import Order, OrderItem


class Fulfillment(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    order = models.ForeignKey(
        Order,
        on_delete=models.PROTECT,
        db_column="order_id",
        related_name="fulfillments",
    )
    status = models.CharField(max_length=30)
    delivery_method = models.CharField(
        max_length=50,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"fulfillment"."fulfillments"'
        verbose_name = "Fulfillment"
        verbose_name_plural = "Fulfillments"

    def __str__(self):
        return str(self.id)


class FulfillmentItem(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    fulfillment = models.ForeignKey(
        Fulfillment,
        on_delete=models.PROTECT,
        db_column="fulfillment_id",
        related_name="items",
    )
    order_item = models.ForeignKey(
        OrderItem,
        on_delete=models.PROTECT,
        db_column="order_item_id",
        related_name="fulfillment_items",
    )
    quantity = models.IntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"fulfillment"."fulfillment_items"'
        verbose_name = "Fulfillment Item"
        verbose_name_plural = "Fulfillment Items"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0),
                name="ck_fulfillment_item_quantity_positive",
            ),
        ]

    def __str__(self):
        return f"{self.fulfillment} - {self.order_item}"