"""
Carts and checkouts. A shopper fills a cart, then starts a checkout, which
freezes the cart's lines and prices. Turning a checkout into an order is the
orders app's job (orders.services.create_order).
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.catalog.services import ACTIVE as PRODUCT_ACTIVE

from .models import Cart, CartItem, Checkout, CheckoutItem

CART_OPEN, CART_CHECKED_OUT = "open", "checked_out"
CHECKOUT_OPEN, CHECKOUT_COMPLETED, CHECKOUT_ABANDONED = "open", "completed", "abandoned"

ZERO = Decimal("0.00")


def _money(value):
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def active_items(cart):
    return cart.items.filter(is_deleted=False).select_related("product", "variant")


def get_or_create_cart(store, session_id=None, customer=None, currency="INR"):
    """The shopper's open cart: by customer if signed in, otherwise by session."""
    if customer is None and not session_id:
        raise ValidationError({"session_id": "A session id or customer is required."})

    carts = Cart.objects.filter(store=store, status=CART_OPEN, is_deleted=False)
    cart = carts.filter(customer=customer).first() if customer else carts.filter(session_id=session_id).first()

    if cart is None:
        cart = Cart.objects.create(
            store=store,
            customer=customer,
            session_id=session_id,
            currency=currency,
            status=CART_OPEN,
            is_deleted=False,
        )
    return cart


def _open_cart(cart):
    cart = Cart.objects.select_for_update().get(pk=cart.pk, is_deleted=False)
    if cart.status != CART_OPEN:
        raise ValidationError("This cart is no longer open.")
    return cart


@transaction.atomic
def add_item(cart, product, quantity, variant=None):
    """Add a product to the cart; adding it again raises the quantity."""
    cart = _open_cart(cart)

    if quantity < 1:
        raise ValidationError({"quantity": "Must be at least 1."})
    if product.store_id != cart.store_id or product.status != PRODUCT_ACTIVE:
        raise ValidationError({"product": "This product isn't available."})
    if variant is not None and variant.product_id != product.pk:
        raise ValidationError({"variant": f"Doesn't belong to {product.title}."})
    if variant is None:
        variant = product.variants.order_by("created_at").first()

    item = CartItem.objects.filter(cart=cart, product=product, variant=variant, is_deleted=False).first()

    if item:
        item.quantity += quantity
        item.save(update_fields=["quantity", "updated_at"])
    else:
        item = CartItem.objects.create(
            cart=cart,
            product=product,
            variant=variant,
            quantity=quantity,
            unit_price=_money(variant.price if variant else 0),
            is_deleted=False,
        )
    return item


@transaction.atomic
def set_quantity(cart, item_id, quantity):
    """Set a line's quantity; 0 removes it."""
    cart = _open_cart(cart)
    item = cart.items.filter(pk=item_id, is_deleted=False).first()

    if item is None:
        raise ValidationError({"item": "Not in this cart."})
    if quantity < 0:
        raise ValidationError({"quantity": "Must be 0 or more."})

    if quantity == 0:
        item.is_deleted = True
        item.deleted_at = timezone.now()
        item.save(update_fields=["is_deleted", "deleted_at", "updated_at"])
    else:
        item.quantity = quantity
        item.save(update_fields=["quantity", "updated_at"])
    return item


@transaction.atomic
def clear_cart(cart):
    cart = _open_cart(cart)
    cart.items.filter(is_deleted=False).update(is_deleted=True, deleted_at=timezone.now())
    return cart


def cart_subtotal(cart):
    return sum((i.unit_price * i.quantity for i in active_items(cart)), ZERO)


@transaction.atomic
def start_checkout(cart, email=None, shipping_address=None, billing_address=None):
    """Freeze the cart's lines into a checkout. Reuses the cart's open checkout."""
    cart = _open_cart(cart)
    items = list(active_items(cart))

    if not items:
        raise ValidationError("The cart is empty.")

    checkout = Checkout.objects.filter(cart=cart, status=CHECKOUT_OPEN, is_deleted=False).first()

    if checkout is None:
        checkout = Checkout.objects.create(
            store=cart.store,
            cart=cart,
            customer=cart.customer,
            currency=cart.currency,
            subtotal=ZERO,
            discount_amount=ZERO,
            tax_amount=ZERO,
            shipping_amount=ZERO,
            total_amount=ZERO,
            status=CHECKOUT_OPEN,
            is_deleted=False,
        )
    else:
        checkout.items.all().delete()

    subtotal = ZERO
    for item in items:
        line_total = item.unit_price * item.quantity
        CheckoutItem.objects.create(
            checkout=checkout,
            product=item.product,
            variant=item.variant,
            product_name=item.product.title,
            sku=item.variant.sku if item.variant else None,
            quantity=item.quantity,
            unit_price=item.unit_price,
            total_price=line_total,
            is_deleted=False,
        )
        subtotal += line_total

    checkout.subtotal = subtotal
    checkout.total_amount = subtotal
    checkout.abandoned_at = None
    update_checkout(checkout, email=email, shipping_address=shipping_address, billing_address=billing_address)
    return checkout


@transaction.atomic
def update_checkout(checkout, email=None, shipping_address=None, billing_address=None):
    """Contact and address details; totals are recomputed from the lines."""
    checkout = Checkout.objects.select_for_update().get(pk=checkout.pk)

    if checkout.status != CHECKOUT_OPEN:
        raise ValidationError("This checkout is no longer open.")

    if email is not None:
        checkout.email = email
    if shipping_address is not None:
        checkout.shipping_address = shipping_address
    if billing_address is not None:
        checkout.billing_address = billing_address

    checkout.subtotal = sum((i.total_price for i in checkout.items.filter(is_deleted=False)), ZERO)
    checkout.total_amount = max(
        checkout.subtotal - checkout.discount_amount + checkout.tax_amount + checkout.shipping_amount, ZERO
    )
    checkout.save()
    return checkout


@transaction.atomic
def complete_checkout(checkout):
    """The shopper paid and an order exists: close the checkout and its cart."""
    checkout = Checkout.objects.select_for_update().get(pk=checkout.pk)

    if checkout.status == CHECKOUT_COMPLETED:
        raise ValidationError("This checkout is already completed.")

    now = timezone.now()
    checkout.status = CHECKOUT_COMPLETED
    if checkout.abandoned_at:
        checkout.recovery_status, checkout.recovered_at = "recovered", now
    checkout.save()

    Cart.objects.filter(pk=checkout.cart_id).update(status=CART_CHECKED_OUT, updated_at=now)
    return checkout


def mark_abandoned(store, idle_for):
    """Flag open checkouts untouched for `idle_for` (a timedelta). Returns how many."""
    now = timezone.now()
    return Checkout.objects.filter(
        store=store,
        status=CHECKOUT_OPEN,
        is_deleted=False,
        abandoned_at__isnull=True,
        updated_at__lt=now - idle_for,
    ).update(abandoned_at=now, recovery_status="pending")
