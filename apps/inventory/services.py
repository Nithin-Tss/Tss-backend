"""
Stock levels. Every change to stock goes through here, so the numbers stay
consistent:  on_hand = available + committed + unavailable

- available:  can be sold
- committed:  sold (in an open order) but not shipped yet
- on_hand:    physically in the location
A variant with no inventory rows is "not tracked": it never blocks a sale.
"""
from django.db import transaction
from rest_framework.exceptions import ValidationError

from .models import InventoryItem


def _item(variant, location):
    item = InventoryItem.objects.select_for_update().filter(variant=variant, location=location).first()
    if item is None:
        item = InventoryItem.objects.create(
            variant=variant, location=location,
            available=0, committed=0, unavailable=0, on_hand=0, incoming=0,
        )
    return item


def _save(item):
    item.on_hand = item.available + item.committed + item.unavailable
    item.save(update_fields=["available", "committed", "unavailable", "on_hand", "incoming", "updated_at"])
    return item


def is_tracked(variant):
    return InventoryItem.objects.filter(variant=variant).exists()


def _label(variant):
    return variant.sku or variant.product.title


@transaction.atomic
def set_available(variant, location, available):
    if available < 0:
        raise ValidationError({"available": "Must be 0 or more."})
    item = _item(variant, location)
    item.available = available
    return _save(item)


@transaction.atomic
def adjust(variant, location, delta):
    item = _item(variant, location)
    if item.available + delta < 0:
        raise ValidationError(f"Only {item.available} available of {_label(variant)} at {location.name}.")
    item.available += delta
    return _save(item)


@transaction.atomic
def commit(variant, quantity):
    """An order was placed: move stock from available to committed."""
    if variant is None or not is_tracked(variant):
        return
    items = list(InventoryItem.objects.select_for_update().filter(variant=variant).order_by("-available"))
    total = sum(i.available for i in items)
    if total < quantity:
        raise ValidationError(f"Only {total} left of {_label(variant)}.")
    for item in items:
        take = min(item.available, quantity)
        item.available -= take
        item.committed += take
        _save(item)
        quantity -= take
        if quantity == 0:
            break


@transaction.atomic
def release(variant, quantity):
    """An order was cancelled: move committed stock back to available."""
    if variant is None:
        return
    for item in InventoryItem.objects.select_for_update().filter(variant=variant, committed__gt=0):
        give = min(item.committed, quantity)
        item.committed -= give
        item.available += give
        _save(item)
        quantity -= give
        if quantity == 0:
            break


@transaction.atomic
def ship(variant, quantity):
    """Items left the building: committed stock is no longer on hand."""
    if variant is None:
        return
    for item in InventoryItem.objects.select_for_update().filter(variant=variant, committed__gt=0):
        take = min(item.committed, quantity)
        item.committed -= take
        _save(item)
        quantity -= take
        if quantity == 0:
            break


@transaction.atomic
def receive(variant, location, quantity):
    """New stock arrived (purchase order or transfer)."""
    item = _item(variant, location)
    item.available += quantity
    item.incoming = max(0, item.incoming - quantity)
    return _save(item)


@transaction.atomic
def move(variant, source, destination, quantity):
    adjust(variant, source, -quantity)
    receive(variant, destination, quantity)
