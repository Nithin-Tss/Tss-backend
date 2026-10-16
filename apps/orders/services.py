"""
Orders. Creating or cancelling an order also updates stock (inventory app),
in the same database transaction: both happen, or neither does.
"""
from decimal import Decimal

from django.db import transaction
from django.dispatch import Signal
from rest_framework.exceptions import ValidationError

from apps.core.numbering import next_number
from apps.inventory import services as inventory

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


@transaction.atomic
def create_order(
    store,
    lines,
    customer=None,
    currency="INR",
    discount_amount=ZERO,
    tax_amount=ZERO,
    shipping_amount=ZERO,
    sales_channel=None,
    delivery_method=None,
    user=None,
):
    """
    lines: [{"product", "variant", "quantity", "unit_price" (optional),
             "discount_amount" (optional)}]
    Prices default to the variant's current price. Stock is committed.
    """
    if not lines:
        raise ValidationError({"items": "An order needs at least one item."})
    if customer is not None and customer.store_id != store.pk:
        raise ValidationError({"customer": "Customer does not belong to this store."})
    if sales_channel is not None and sales_channel.store_id != store.pk:
        raise ValidationError({"sales_channel": "Sales channel does not belong to this store."})

    tax_amount = _money(tax_amount)
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
    )

    subtotal = line_discounts = ZERO

    for line in lines:
        product, variant, quantity = line["product"], line.get("variant"), line["quantity"]

        if product.store_id != store.pk:
            raise ValidationError({"items": f"{product.title} doesn't belong to this store."})
        if quantity <= 0:
            raise ValidationError({"items": "Item quantities must be greater than zero."})
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

        OrderItem.objects.create(
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

        subtotal += gross
        line_discounts += discount

    if discount_amount > subtotal - line_discounts:
        raise ValidationError({"discount_amount": "The discount can't exceed the remaining subtotal."})

    order.subtotal = subtotal
    order.discount_amount = line_discounts + _money(discount_amount)
    order.total = max(subtotal - order.discount_amount + order.tax_amount + order.shipping_amount, ZERO)
    order.save(update_fields=["subtotal", "discount_amount", "total", "updated_at"])

    transaction.on_commit(lambda: order_placed.send(sender=Order, order=order))
    return order


@transaction.atomic
def cancel_order(order, user=None):
    order = Order.objects.select_for_update().get(pk=order.pk)

    if order.status == CANCELLED:
        raise ValidationError("This order is already cancelled.")
    if order.fulfillment_status != UNFULFILLED:
        raise ValidationError("Orders with shipped items can't be cancelled.")

    for item in order.items.filter(is_deleted=False):
        inventory.release(item.variant, item.quantity, user_id=getattr(user, "pk", None), reference_id=order.pk)

    order.status = CANCELLED
    order.save(update_fields=["status", "updated_at"])

    transaction.on_commit(lambda: order_cancelled.send(sender=Order, order=order))
    return order
