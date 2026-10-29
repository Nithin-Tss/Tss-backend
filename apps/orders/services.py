"""
Orders. Creating or cancelling an order also updates stock (inventory app),
in the same database transaction: both happen, or neither does.
"""
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.dispatch import Signal
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.commerce import tax as tax_rules
from apps.core.numbering import next_number
from apps.inventory import services as inventory

from apps.catalog.services import ARCHIVED

from .models import Order, OrderItem

OPEN, CANCELLED, CLOSED = "open", "cancelled", "closed"
UNFULFILLED, PARTIAL, FULFILLED = "unfulfilled", "partial", "fulfilled"

# Announcements other parts of the system can listen to (emails, analytics).
order_placed = Signal()      # sender=Order, order=...
order_cancelled = Signal()   # sender=Order, order=...

ZERO = Decimal("0.00")


def _money(value):
    try:
        amount = Decimal(str(value or 0)).quantize(Decimal("0.01"))
    except (ArithmeticError, TypeError, ValueError):
        raise ValidationError({"amount": "Enter a valid monetary amount."})
    if not amount.is_finite():
        raise ValidationError({"amount": "Enter a valid monetary amount."})
    return amount


def destination(customer=None, checkout=None):
    """(country, region) tax is charged for: the checkout's shipping address, else the customer's."""
    address = checkout.shipping_address if checkout is not None else None
    if isinstance(address, dict) and address.get("country"):
        return address.get("country"), address.get("state") or address.get("region") or address.get("province")
    if customer is not None:
        default = next((a for a in customer.addresses.all() if a.is_default), None) or customer.addresses.first()
        if default is not None:
            return default.country, default.state
        return customer.country, customer.state
    return None, None


def _apply_tax(order, items, lines, order_discount, customer, checkout, explicit):
    """Set tax_amount on the order and on each of its items."""
    store = order.store
    amounts = [i.total for i in items]
    taxable = [bool(i.product.is_taxable) for i in items]

    if explicit:
        # Keep the given amount; the lines carry their share of it
        shares = tax_rules.allocate(order.tax_amount, [a if t else ZERO for a, t in zip(amounts, taxable)])
    else:
        country, region = destination(customer, checkout)
        result = tax_rules.calculate(
            store,
            [{"amount": a, "taxable": t} for a, t in zip(amounts, taxable)],
            discount=order_discount, shipping=order.shipping_amount, country=country, region=region,
            exempt=bool(customer is not None and customer.tax_exempt),
        )
        order.tax_amount = result.total
        shares = result.line_taxes

    for item, share in zip(items, shares):
        if share:
            item.tax_amount = share
            item.save(update_fields=["tax_amount", "updated_at"])


@transaction.atomic
def create_order(
    store,
    lines,
    customer=None,
    currency=None,
    discount_amount=ZERO,
    tax_amount=None,
    shipping_amount=ZERO,
    sales_channel=None,
    delivery_method=None,
    user=None,
    checkout=None,
):
    """
    lines: [{"product", "variant", "quantity", "unit_price" (optional),
             "discount_amount" (optional)}]
    Prices default to the variant's current price. Stock is committed.
    checkout: the storefront checkout being placed; the order links to it and
    keeps a copy of its email and addresses.
    tax_amount: left out, tax follows the store's tax rules for the checkout's
    address (or the customer's) and is split over the lines. A given amount is
    kept as is and shared over the taxable lines.
    """
    if not lines:
        raise ValidationError({"items": "An order needs at least one item."})
    currency = (currency or store.currency).upper()
    if currency != store.currency:
        raise ValidationError({"currency_code": f"This store sells in {store.currency}."})
    if customer is not None and customer.store_id != store.pk:
        raise ValidationError({"customer": "Customer does not belong to this store."})
    if sales_channel is not None and sales_channel.store_id != store.pk:
        raise ValidationError({"sales_channel": "Sales channel does not belong to this store."})
    if checkout is not None and checkout.store_id != store.pk:
        raise ValidationError({"checkout": "Checkout does not belong to this store."})

    explicit_tax = tax_amount is not None
    tax_amount = _money(tax_amount) if explicit_tax else ZERO
    shipping_amount = _money(shipping_amount)
    discount_amount = _money(discount_amount)
    if min(tax_amount, shipping_amount, discount_amount) < ZERO:
        raise ValidationError("Tax, shipping, and discount amounts can't be negative.")

    order = Order.objects.create(
        store=store,
        order_number=next_number(
            store, Order, "order_number", store.order_prefix, suffix=store.order_suffix,
        ),
        customer=customer,
        sales_channel=sales_channel,
        currency_code=currency,
        subtotal=ZERO,
        discount_amount=ZERO,
        tax_amount=tax_amount,
        shipping_amount=shipping_amount,
        total=ZERO,
        status=OPEN,
        fulfillment_status=UNFULFILLED,
        delivery_method=delivery_method,
        checkout=checkout,
        shipping_address=checkout.shipping_address if checkout else None,
        billing_address=checkout.billing_address if checkout else None,
        customer_email=(checkout.email or None) if checkout else None,
    )

    subtotal = line_discounts = ZERO
    items = []

    # Lock the stock of every line's variant, in one fixed order, before any of it is changed
    inventory.lock_stock(store.pk, [
        getattr(line.get("variant") or line["product"].variants.order_by("created_at").first(), "pk", None)
        for line in lines
    ])

    for line in lines:
        product, variant, quantity = line["product"], line.get("variant"), line["quantity"]

        if product.store_id != store.pk:
            raise ValidationError({"items": f"{product.title} doesn't belong to this store."})
        if quantity <= 0:
            raise ValidationError({"items": "Item quantities must be greater than zero."})
        if product.status == ARCHIVED:
            raise ValidationError({"items": f"{product.title} is archived, so it can't be ordered."})
        if variant is not None and variant.product_id != product.pk:
            raise ValidationError({"items": f"{variant.sku or 'A variant'} doesn't belong to {product.title}."})
        if variant is not None and variant.product.store_id != store.pk:
            raise ValidationError({"items": "The variant doesn't belong to this store."})
        if variant is None:
            variant = product.variants.order_by("created_at").first()

        unit_price = _money(line.get("unit_price") if line.get("unit_price") is not None else (variant.price if variant else 0))
        discount = _money(line.get("discount_amount"))
        gross = unit_price * quantity
        if unit_price < ZERO or discount < ZERO or discount > gross:
            raise ValidationError(
                {"items": "Prices and discounts must be non-negative, and discounts can't exceed the item subtotal."}
            )

        item = OrderItem.objects.create(
            order=order,
            product=product,
            variant=variant,
            product_name=product.title,
            sku=variant.sku if variant else None,
            quantity=quantity,
            unit_price=unit_price,
            discount_amount=discount,
            tax_amount=ZERO,
            total=max(gross - discount, ZERO),
            is_deleted=False,
        )
        inventory.commit(variant, quantity, user_id=getattr(user, "pk", None), reference_id=order.pk)

        items.append(item)
        subtotal += gross
        line_discounts += discount

    if discount_amount > subtotal - line_discounts:
        raise ValidationError({"discount_amount": "The discount can't exceed the remaining subtotal."})

    order.subtotal = subtotal
    order.discount_amount = line_discounts + _money(discount_amount)

    _apply_tax(order, items, lines, _money(discount_amount), customer, checkout, explicit_tax)
    order.total = tax_rules.grand_total(store, subtotal, order.discount_amount, order.tax_amount, order.shipping_amount)
    order.save(update_fields=["subtotal", "discount_amount", "tax_amount", "total", "updated_at"])

    transaction.on_commit(lambda: order_placed.send(sender=Order, order=order))
    return order


@transaction.atomic
def cancel_order(order, user=None, refuse_if_paid=False):
    order = Order.objects.select_for_update().get(pk=order.pk)

    if order.status == CANCELLED:
        raise ValidationError("This order is already cancelled.")
    if order.fulfillment_status != UNFULFILLED:
        raise ValidationError("Orders with shipped items can't be cancelled.")
    if refuse_if_paid:
        # Checked under the order lock so a payment captured meanwhile can't slip past
        from apps.payments import services as payments

        if payments.paid_total(order) > 0:
            raise ValidationError("This order has been paid. Please contact the store to cancel it.")
    else:
        # Staff cancel: money still held for this order goes back first. If that fails, nothing below runs
        # (no cancel, no stock release) and the whole transaction rolls back.
        from apps.payments import services as payments

        try:
            payments.refund_unrefunded(order, "Order cancelled")
        except ValidationError as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {"payment": exc.detail}
            raise ValidationError({**detail, "order": "The order was not cancelled because the refund failed."})

    _cancel_locked(order, user)
    return order


def customer_cancel_deadline(order):
    """The moment a shopper can no longer cancel this order themselves (settings.ORDER_CUSTOMER_CANCEL_MINUTES)."""
    return order.created_at + timedelta(minutes=settings.ORDER_CUSTOMER_CANCEL_MINUTES)


def customer_cancel_window_text():
    """The window in words for the message shoppers see: "1 hour", "2 hours", "30 minutes"."""
    minutes = settings.ORDER_CUSTOMER_CANCEL_MINUTES
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hour" + ("" if hours == 1 else "s")
    return f"{minutes} minute" + ("" if minutes == 1 else "s")


STOCK_RELEASED = "stock_released"  # Order.metadata: set once the order's unshipped stock went back to available


def release_unshipped_stock(order, user=None, reason="cancel"):
    """
    Put the order's committed stock for units that have NOT shipped back to available, once only
    (Order.metadata["stock_released"] records it). Shipped units are not touched: they are with the
    customer and come back through a return. The caller holds the order row lock. True if released now.
    """
    if (order.metadata or {}).get(STOCK_RELEASED):
        return False
    from apps.fulfillment.services import fulfilled_quantities

    shipped = fulfilled_quantities(order)
    items = list(order.items.filter(is_deleted=False))
    inventory.lock_stock(order.store_id, [item.variant_id for item in items])
    for item in items:
        left = item.quantity - shipped.get(item.pk, 0)
        if left > 0:
            inventory.release(item.variant, left, user_id=getattr(user, "pk", None), reference_id=order.pk)
    order.metadata = {**(order.metadata or {}), STOCK_RELEASED: {"at": timezone.now().isoformat(), "reason": reason}}
    order.save(update_fields=["metadata", "updated_at"])
    return True


def _cancel_locked(order, user=None):
    """Cancel an order whose row the caller has locked: stock back (once), status, unpaid payment, discount use."""
    release_unshipped_stock(order, user, reason="cancel")

    order.status = CANCELLED
    order.save(update_fields=["status", "updated_at"])

    # Void the unpaid payment and give the discount code its use back
    from apps.discounts.models import OrderDiscount
    from apps.payments import services as payments

    payments.cancel_pending(order)
    now = timezone.now()
    OrderDiscount.objects.filter(order=order, is_deleted=False).update(is_deleted=True, deleted_at=now)

    transaction.on_commit(lambda: order_cancelled.send(sender=Order, order=order))


def _return_location(store, variant):
    """Where returned units go when staff do not choose: where the variant already has stock, else the store default."""
    from apps.inventory.models import InventoryItem

    row = InventoryItem.objects.filter(variant=variant).select_related("location").order_by("created_at").first()
    return row.location if row else inventory.default_location(store)


@transaction.atomic
def return_items(order, lines, user=None):
    """
    Staff mark shipped items as returned (a failed delivery, a customer return): the units go back on the shelf.
    lines: [{"order_item": OrderItem, "quantity": int, "location": Location or None}]
    Only shipped units can come back, and each unit only once: what was returned is kept per line in
    OrderItem.metadata["returned_quantity"], checked and updated under the order lock together with the restock.
    When every shipped unit is back: an order with no money received is cancelled (its pending payment voided);
    one that was paid keeps its status and is flagged (Order.metadata["returned"]); refunding stays a staff action.
    """
    from apps.fulfillment.services import fulfilled_quantities
    from apps.payments import services as payments

    order = Order.objects.select_for_update().get(pk=order.pk)
    if order.status == CANCELLED:
        raise ValidationError("This order is cancelled.")
    if not lines:
        raise ValidationError({"items": "Choose at least one item to return."})

    shipped = fulfilled_quantities(order)
    items = {i.pk: i for i in order.items.select_for_update().filter(is_deleted=False)}
    wanted = {}
    for line in lines:
        item = items.get(line["order_item"].pk)
        if item is None:
            raise ValidationError({"items": f"{line['order_item'].product_name} is not in this order."})
        if line["quantity"] <= 0:
            raise ValidationError({"items": "Quantities to return must be more than zero."})
        entry = wanted.setdefault(item.pk, {"item": item, "quantity": 0, "location": line.get("location")})
        entry["quantity"] += line["quantity"]
        entry["location"] = entry["location"] or line.get("location")

    for entry in wanted.values():
        item = entry["item"]
        back = (item.metadata or {}).get("returned_quantity", 0)
        can_return = shipped.get(item.pk, 0) - back
        if entry["quantity"] > can_return:
            if shipped.get(item.pk, 0):
                message = f"Only {max(can_return, 0)} of {item.product_name} can still be returned."
            else:
                message = f"{item.product_name} has not shipped, so it can't be returned."
            raise ValidationError({"items": message})
        if item.variant_id is None:
            raise ValidationError({"items": f"{item.product_name} can no longer be put back in stock."})
        if entry["location"] is not None and entry["location"].store_id != order.store_id:
            raise ValidationError({"location": "That location isn't in this store."})

    inventory.lock_stock(order.store_id, [e["item"].variant_id for e in wanted.values()])
    for entry in wanted.values():
        item = entry["item"]
        # A variant with no stock rows is not tracked: it never had stock to put back, and restocking would
        # start tracking it. Its units still count as returned below, so they can't be returned twice.
        if inventory.is_tracked(item.variant):
            location = entry["location"] or _return_location(order.store, item.variant)
            inventory.restock_refund(
                item.variant, location, entry["quantity"], user_id=getattr(user, "pk", None),
                source="return", reference_id=order.pk, note="Returned",
            )
        metadata = dict(item.metadata or {})
        metadata["returned_quantity"] = metadata.get("returned_quantity", 0) + entry["quantity"]
        item.metadata = metadata
        item.save(update_fields=["metadata", "updated_at"])

    all_items = list(items.values())
    if sum(shipped.values()) > 0 and all(
        (i.metadata or {}).get("returned_quantity", 0) >= shipped.get(i.pk, 0) for i in all_items
    ):
        order.metadata = {**(order.metadata or {}), "returned": {"at": timezone.now().isoformat()}}
        order.save(update_fields=["metadata", "updated_at"])
        if payments.paid_total(order) <= 0:
            _cancel_locked(order, user)  # nothing was paid (e.g. cash on delivery that failed): cancel, void the payment
    return order


def settle_after_refund(order):
    """
    A refund was just recorded for this order (the caller holds the order lock). When everything paid
    has been refunded: an order with nothing shipped is cancelled (its stock goes back); a partly shipped
    one gets only its unshipped units back and stays open; a fully shipped one is left to the return action.
    A partial refund changes nothing.
    """
    from apps.payments import services as payments

    if order.status == CANCELLED or not payments.refunded_in_full(order):
        return
    if order.fulfillment_status == UNFULFILLED:
        _cancel_locked(order)
    else:
        release_unshipped_stock(order, reason="refund")
