import uuid

from django.db import models

from apps.fulfillment.models import Fulfillment
from apps.orders.models import OrderItem
from apps.tenancy.models import Store


class DeliveryGateway(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        db_column="store_id",
        related_name="delivery_gateways",
    )
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=50)
    is_active = models.BooleanField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_deleted = models.BooleanField()
    deleted_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        db_table = '"delivery"."delivery_gateways"'
        verbose_name = "Delivery Gateway"
        verbose_name_plural = "Delivery Gateways"

    def __str__(self):
        return self.name


class Shipment(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        db_column="store_id",
        related_name="shipments",
    )
    fulfillment = models.ForeignKey(
        Fulfillment,
        on_delete=models.PROTECT,
        db_column="fulfillment_id",
        related_name="shipments",
    )
    delivery_gateway = models.ForeignKey(
        DeliveryGateway,
        on_delete=models.PROTECT,
        db_column="delivery_gateway_id",
        related_name="shipments",
    )
    tracking_number = models.CharField(
        max_length=100,
        null=True,
        blank=True,
    )
    status = models.CharField(max_length=30)
    shipped_at = models.DateTimeField(
        null=True,
        blank=True,
    )
    delivered_at = models.DateTimeField(
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"delivery"."shipments"'
        verbose_name = "Shipment"
        verbose_name_plural = "Shipments"

    def __str__(self):
        return str(self.id)


class ShipmentItem(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    shipment = models.ForeignKey(
        Shipment,
        on_delete=models.PROTECT,
        db_column="shipment_id",
        related_name="items",
    )
    order_item = models.ForeignKey(
        OrderItem,
        on_delete=models.PROTECT,
        db_column="order_item_id",
        related_name="shipment_items",
    )
    quantity = models.IntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_deleted = models.BooleanField()
    deleted_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        db_table = '"delivery"."shipment_items"'
        verbose_name = "Shipment Item"
        verbose_name_plural = "Shipment Items"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0),
                name="ck_shipment_item_quantity_positive",
            ),
        ]

    def __str__(self):
        return f"{self.shipment} - {self.order_item}"


class TrackingEvent(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    shipment = models.ForeignKey(
        Shipment,
        on_delete=models.PROTECT,
        db_column="shipment_id",
        related_name="tracking_events",
    )
    status = models.CharField(max_length=30)
    location = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )
    description = models.TextField(
        null=True,
        blank=True,
    )
    event_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_deleted = models.BooleanField()
    deleted_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        db_table = '"delivery"."tracking_events"'
        verbose_name = "Tracking Event"
        verbose_name_plural = "Tracking Events"

    def __str__(self):
        return f"{self.shipment} - {self.status}"