"""
Stock levels. EVERY change to stock goes through adjust_inventory(), so the numbers
stay consistent and every change leaves a history row.

    on_hand = available + committed + unavailable      (calculated, kept in step on every change)

- available:    can be sold
- committed:    sold (in an open order) but not shipped yet
- unavailable:  set aside (damaged, quality control, safety stock, other)
- incoming:     ordered from a supplier, not arrived yet
A variant with no inventory rows is "not tracked": it never blocks a sale.
"""
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.db.models.signals import post_save
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.catalog.models import ProductVariant

from .models import InventoryHistory, InventoryItem, Location

# Where stock goes when none is chosen (imports) and the store has no location yet
DEFAULT_LOCATION = "Main warehouse"

# What each reason does to (available, committed, unavailable, incoming), per unit moved.
# "manual" reasons change available only (on hand follows); the amount may be + or -.
MANUAL_REASONS = {
    "correction": "Correction",
    "cycle_count": "Cycle count",
    "received": "Received",
    "return_restock": "Customer return",
    "shrinkage": "Shrinkage",
    "product_edit": "Product page edit",
    "import": "Import",
    "other": "Other",
}

# Movement reasons: the amount is a positive number of units
EFFECTS = {
    "order_placed": (-1, +1, 0, 0),
    "order_cancelled": (+1, -1, 0, 0),
    "order_fulfilled": (0, -1, 0, 0),
    "refund_restock": (+1, 0, 0, 0),
    "incoming_received": (+1, 0, 0, -1),
}

UNAVAILABLE_REASONS = ("damaged", "quality_control", "safety_stock", "other")

# Marking stock unavailable (available -> unavailable) and putting it back, per reason
for _why in UNAVAILABLE_REASONS:
    EFFECTS[f"unavailable_{_why}"] = (-1, 0, +1, 0)
    EFFECTS[f"unavailable_released_{_why}"] = (+1, 0, -1, 0)

REASONS = {**{r: (1, 0, 0, 0) for r in MANUAL_REASONS}, **EFFECTS}


def _whole(value, name):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError({name: "Must be a whole number."})
    return value


def _label(variant):
    return variant.sku or variant.product.title


def _locked_item(variant, location):
    """The stock row for this variant at this location, locked until the transaction ends."""
    qs = InventoryItem.objects.select_for_update().filter(variant=variant, location=location)
    item = qs.first()
    if item is not None:
        return item
    try:
        with transaction.atomic():
            InventoryItem.objects.create(
                variant=variant, location=location,
                available=0, committed=0, unavailable=0, on_hand=0, incoming=0,
            )
    except IntegrityError:  # two first writes at the same moment: the other one won, use its row
        pass
    return qs.get()


def adjust_inventory(
    store_id,
    variant_id,
    location_id,
    reason,
    user_id=None,
    delta=None,
    new_quantity=None,
    source="admin",
    reference_id=None,
    note="",
):
    """
    The one way to change stock. Returns the updated InventoryItem.

    reason:       a key of REASONS (see above). It decides which numbers move.
    delta:        how much to change by. A signed change for the manual reasons; a positive
                  number of units for order_placed, order_cancelled, order_fulfilled,
                  refund_restock, incoming_received and unavailable_*.
    new_quantity: manual reasons only: the new available total (the change is worked out).
    source:       where the change came from ("admin", "product_page", "order", "import", ...).
    reference_id: the order, fulfillment, purchase order... behind the change.

    One transaction, row locked. Blocked, with nothing saved, when available would go
    below 0 (unless the variant has continue_selling on) or any other number below 0.
    Every query is limited to store_id.
    """
    if reason not in REASONS:
        raise ValidationError({"reason": f"Unknown reason '{reason}'."})
    manual = reason in MANUAL_REASONS
    if (delta is None) == (new_quantity is None):
        raise ValidationError("Send either a change or a new quantity.")
    if new_quantity is not None and not manual:
        raise ValidationError({"new_quantity": "A new total can only be set by a manual adjustment."})
    if delta is not None:
        _whole(delta, "delta")
        if delta == 0:
            raise ValidationError({"delta": "The change must not be 0."})
        if not manual and delta < 0:
            raise ValidationError({"delta": "Must be more than 0 for this reason."})
    if new_quantity is not None:
        _whole(new_quantity, "new_quantity")
        if new_quantity < 0:
            raise ValidationError({"new_quantity": "Must be 0 or more."})

    with transaction.atomic():
        variant = (
            ProductVariant.objects.select_related("product")
            .filter(pk=variant_id, product__store_id=store_id)
            .first()
        )
        location = Location.objects.filter(pk=location_id, store_id=store_id).first()
        if variant is None:
            raise ValidationError({"variant": "That product isn't in this store."})
        if location is None:
            raise ValidationError({"location": "That location isn't in this store."})

        item = _locked_item(variant, location)
        before = (item.available, item.committed, item.unavailable, item.incoming)

        if new_quantity is not None:
            delta = new_quantity - item.available
            if delta == 0:
                return item  # nothing to change, nothing to record

        da, dc, du, di = (m * delta for m in REASONS[reason])
        available, committed, unavailable = item.available + da, item.committed + dc, item.unavailable + du
        incoming = max(0, item.incoming + di)  # more arrived than was expected: not an error

        if available < 0 and not variant.continue_selling:
            if reason == "order_placed":
                raise ValidationError(f"Only {item.available} left of {_label(variant)}.")
            raise ValidationError(
                f"Only {item.available} available of {_label(variant)} at {location.name}. "
                f"Can't take away {-da}."
            )
        if committed < 0:
            raise ValidationError(f"Only {item.committed} committed of {_label(variant)} at {location.name}.")
        if unavailable < 0:
            raise ValidationError(f"Only {item.unavailable} unavailable of {_label(variant)} at {location.name}.")

        on_hand = available + committed + unavailable
        # Write only if nobody changed the row since we read it, so two changes at the same
        # moment are both counted (the row lock already guarantees it on PostgreSQL)
        changed = InventoryItem.objects.filter(
            pk=item.pk,
            available=before[0], committed=before[1], unavailable=before[2], incoming=before[3],
        ).update(
            available=available, committed=committed, unavailable=unavailable,
            incoming=incoming, on_hand=on_hand, updated_at=timezone.now(),
        )
        if not changed:
            raise ValidationError("This stock was just changed by someone else. Try again.")
        item.available, item.committed, item.unavailable = available, committed, unavailable
        item.incoming, item.on_hand = incoming, on_hand

        InventoryHistory.objects.create(
            store_id=store_id,
            variant=variant,
            location=location,
            reason=reason,
            source=source,
            reference_id=str(reference_id) if reference_id is not None else None,
            note=(note or "")[:500],
            user_id=user_id,
            before=before[0],
            after=available,
            change=da,
            committed_change=dc,
            unavailable_change=du,
            committed=committed,
            unavailable=unavailable,
            on_hand=on_hand,
            incoming=incoming,
        )
        # .update() sends no signal; automated collections and others listen for stock changes
        post_save.send(sender=InventoryItem, instance=item, created=False, raw=False, using="default")
        return item


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def sellable(variant):
    """Units of `variant` a shopper can buy: available stock over all locations, 0 with no stock record."""
    return InventoryItem.objects.filter(variant=variant).aggregate(total=Sum("available"))["total"] or 0


def default_location(store):
    """The store's first location, created if it has none."""
    location = Location.objects.filter(store=store).order_by("created_at").first()
    return location or Location.objects.create(store=store, name=DEFAULT_LOCATION)


def is_tracked(variant):
    return InventoryItem.objects.filter(variant=variant).exists()


def _store_id(variant):
    return variant.product.store_id


def _call(variant, location, reason, **kwargs):
    return adjust_inventory(_store_id(variant), variant.pk, location.pk, reason, **kwargs)


# ---------------------------------------------------------------------------
# What the rest of the app calls. Each one is adjust_inventory() with a reason.
# ---------------------------------------------------------------------------


def set_available(variant, location, available, user_id=None, source="admin", reason="correction",
                  reference_id=None, note=""):
    """Make the available total exactly `available` (a manual adjustment)."""
    if available < 0:
        raise ValidationError({"available": "Must be 0 or more."})
    return _call(variant, location, reason, new_quantity=available, user_id=user_id,
                 source=source, reference_id=reference_id, note=note)


def adjust(variant, location, delta, user_id=None, source="admin", reason="correction",
           reference_id=None, note=""):
    """Change available (and on hand) by `delta`, + or -."""
    return _call(variant, location, reason, delta=delta, user_id=user_id,
                 source=source, reference_id=reference_id, note=note)


def mark_unavailable(variant, location, quantity, why, user_id=None, source="admin", reference_id=None, note=""):
    """Move stock from available to unavailable. why: damaged, quality_control, safety_stock or other."""
    if why not in UNAVAILABLE_REASONS:
        raise ValidationError({"reason": f"Choose one of: {', '.join(UNAVAILABLE_REASONS)}."})
    return _call(variant, location, f"unavailable_{why}", delta=quantity, user_id=user_id,
                 source=source, reference_id=reference_id, note=note)


def release_unavailable(variant, location, quantity, why, user_id=None, source="admin", reference_id=None, note=""):
    """Move stock from unavailable back to available (why: the reason it was set aside for)."""
    if why not in UNAVAILABLE_REASONS:
        raise ValidationError({"reason": f"Choose one of: {', '.join(UNAVAILABLE_REASONS)}."})
    return _call(variant, location, f"unavailable_released_{why}", delta=quantity, user_id=user_id,
                 source=source, reference_id=reference_id, note=note)


def restock_refund(variant, location, quantity, user_id=None, source="refund", reference_id=None, note=""):
    """A refunded item came back to the shelf: available and on hand go up."""
    return _call(variant, location, "refund_restock", delta=quantity, user_id=user_id,
                 source=source, reference_id=reference_id, note=note)


def _split(variant, quantity, field, reason, user_id, source, reference_id):
    """Spread `quantity` over the variant's locations (most `field` first) through adjust_inventory."""
    items = list(
        InventoryItem.objects.select_for_update()
        .filter(variant=variant)
        .select_related("location")
        .order_by(f"-{field}")
    )
    left = quantity
    for item in items:
        take = min(getattr(item, field), left)
        if take <= 0:
            continue
        _call(variant, item.location, reason, delta=take, user_id=user_id,
              source=source, reference_id=reference_id)
        left -= take
        if left == 0:
            break
    return left


@transaction.atomic
def commit(variant, quantity, user_id=None, source="order", reference_id=None):
    """An order was placed: move stock from available to committed."""
    if variant is None or not is_tracked(variant):
        return
    items = list(
        InventoryItem.objects.select_for_update().filter(variant=variant).select_related("location")
        .order_by("-available")
    )
    total = sum(i.available for i in items)
    if total < quantity and not variant.continue_selling:
        raise ValidationError(f"Only {total} left of {_label(variant)}.")
    left = quantity
    for i, item in enumerate(items):
        # With continue_selling on, whatever is not in stock goes on the first location
        take = left if (variant.continue_selling and i == 0 and total < quantity) else min(max(item.available, 0), left)
        if take <= 0:
            continue
        _call(variant, item.location, "order_placed", delta=take, user_id=user_id,
              source=source, reference_id=reference_id)
        left -= take
        if left == 0:
            break


@transaction.atomic
def release(variant, quantity, user_id=None, source="order", reference_id=None):
    """An order was cancelled: move committed stock back to available."""
    if variant is None:
        return
    _split(variant, quantity, "committed", "order_cancelled", user_id, source, reference_id)


@transaction.atomic
def ship(variant, quantity, user_id=None, source="fulfillment", reference_id=None):
    """Items left the building: committed stock is no longer on hand."""
    if variant is None:
        return
    _split(variant, quantity, "committed", "order_fulfilled", user_id, source, reference_id)


def receive(variant, location, quantity, user_id=None, source="purchase_order", reference_id=None):
    """New stock arrived (purchase order or transfer)."""
    return _call(variant, location, "incoming_received", delta=quantity, user_id=user_id,
                 source=source, reference_id=reference_id)


@transaction.atomic
def move(variant, source_location, destination, quantity, user_id=None, reference_id=None):
    adjust(variant, source_location, -quantity, user_id=user_id, source="transfer",
           reason="correction", reference_id=reference_id)
    receive(variant, destination, quantity, user_id=user_id, source="transfer", reference_id=reference_id)
