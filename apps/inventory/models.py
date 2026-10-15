import uuid

from django.conf import settings
from django.db import models

from apps.catalog.models import ProductVariant
from apps.tenancy.models import Store
from django.core.exceptions import ValidationError



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

    
    def clean(self):
        super().clean()

        errors = {}

        quantity_fields = [
            "unavailable",
            "committed",
            "available",
            "on_hand",
            "incoming",
        ]

        for field in quantity_fields:
            value = getattr(self, field, None)

            if value is not None and value < 0:
                errors[field] = "This quantity cannot be negative."

        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.variant} - {self.location}"


class AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Stock history can't be edited.")

    def delete(self):
        raise ValidationError("Stock history can't be deleted.")


class InventoryHistory(models.Model):
    """One row per stock change, written by inventory.services.adjust_inventory. Never edited or deleted."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    store = models.ForeignKey(
        Store, on_delete=models.PROTECT, db_column="store_id", related_name="inventory_history"
    )
    variant = models.ForeignKey(
        ProductVariant, on_delete=models.PROTECT, db_column="variant_id", related_name="inventory_history"
    )
    location = models.ForeignKey(
        Location, on_delete=models.PROTECT, db_column="location_id", related_name="inventory_history"
    )

    reason = models.CharField(max_length=40)
    source = models.CharField(max_length=40)
    reference_id = models.CharField(max_length=64, null=True, blank=True)
    note = models.CharField(max_length=500, blank=True, default="")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        db_column="user_id",
        related_name="+",
    )

    # Available before/after, and the change to it
    before = models.IntegerField()
    after = models.IntegerField()
    change = models.IntegerField()

    # How much committed and unavailable moved (for the breakdowns), and where all numbers ended up
    committed_change = models.IntegerField(default=0)
    unavailable_change = models.IntegerField(default=0)
    committed = models.IntegerField()
    unavailable = models.IntegerField()
    on_hand = models.IntegerField()
    incoming = models.IntegerField()

    created_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        db_table = '"inventory"."inventory_history"'
        verbose_name = "Inventory History"
        verbose_name_plural = "Inventory History"
        indexes = [
            models.Index(fields=["store", "variant", "-created_at"], name="invhist_variant_idx"),
            models.Index(fields=["store", "reference_id"], name="invhist_reference_idx"),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Stock history can't be edited.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Stock history can't be deleted.")

    def __str__(self):
        return f"{self.variant} {self.change:+d} ({self.reason})"


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

        indexes = [
            models.Index(
                fields=["store", "-created_at"],
                name="trf_store_created_idx",
            ),
        ]

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