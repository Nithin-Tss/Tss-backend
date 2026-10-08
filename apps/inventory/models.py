import uuid

from django.db import models

from apps.catalog.models import ProductVariant
from apps.tenancy.models import Store


class Location(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        db_column="store_id",
        related_name="inventory_locations",
    )

    name = models.CharField(max_length=255)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = '"inventory"."locations"'
        verbose_name = "Inventory Location"
        verbose_name_plural = "Inventory Locations"

    def __str__(self):
        return self.name


class InventoryItem(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    variant = models.ForeignKey(
        ProductVariant,
        on_delete=models.PROTECT,
        db_column="variant_id",
        related_name="inventory_items",
    )

    location = models.ForeignKey(
        Location,
        on_delete=models.PROTECT,
        db_column="location_id",
        related_name="inventory_items",
    )

    unavailable = models.IntegerField()

    committed = models.IntegerField()

    available = models.IntegerField()

    on_hand = models.IntegerField()

    incoming = models.IntegerField()

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"inventory"."inventory_items"'
        verbose_name = "Inventory Item"
        verbose_name_plural = "Inventory Items"

        constraints = [
            models.UniqueConstraint(
                fields=["variant", "location"],
                name="uq_inventory_item_variant_location",
            )
        ]

    def __str__(self):
        return f"{self.variant} - {self.location}"


class Transfer(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        db_column="store_id",
        related_name="inventory_transfers",
    )

    transfer_number = models.CharField(max_length=50)

    source_location = models.ForeignKey(
        Location,
        on_delete=models.PROTECT,
        db_column="source_location_id",
        related_name="outgoing_transfers",
    )

    destination_location = models.ForeignKey(
        Location,
        on_delete=models.PROTECT,
        db_column="destination_location_id",
        related_name="incoming_transfers",
    )

    status = models.CharField(max_length=30)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"inventory"."transfers"'
        verbose_name = "Inventory Transfer"
        verbose_name_plural = "Inventory Transfers"

    def __str__(self):
        return self.transfer_number


class TransferItem(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    transfer = models.ForeignKey(
        Transfer,
        on_delete=models.PROTECT,
        db_column="transfer_id",
        related_name="items",
    )

    variant = models.ForeignKey(
        ProductVariant,
        on_delete=models.PROTECT,
        db_column="variant_id",
        related_name="transfer_items",
    )

    quantity = models.IntegerField()

    class Meta:
        db_table = '"inventory"."transfer_items"'
        verbose_name = "Transfer Item"
        verbose_name_plural = "Transfer Items"

        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0),
                name="ck_transfer_item_quantity_positive",
            )
        ]

    def __str__(self):
        return f"{self.transfer} - {self.variant}"