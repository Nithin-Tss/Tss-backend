"""
Carts and checkouts. A shopper fills a cart, then starts a checkout, which
freezes the cart's lines and prices. Turning a checkout into an order is the
orders app's job (orders.services.create_order).
"""
from decimal import ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.catalog.services import published_products
from apps.discounts import services as discounts
from apps.discounts.models import Discount, OrderDiscount
from apps.customers.models import Customer
from apps.inventory.services import sellable
from apps.orders import services as orders
from apps.orders.models import Order
from apps.payments import services as payments

from .models import Cart, CartItem, Checkout, CheckoutItem

CART_OPEN, CART_CHECKED_OUT, CART_MERGED, CART_EXPIRED = "open", "checked_out", "merged", "expired"
CHECKOUT_OPEN, CHECKOUT_COMPLETED, CHECKOUT_ABANDONED = "open", "completed", "abandoned"

ZERO = Decimal("0.00")
PAYMENT_METHODS = ("cod",)
MAX_OPEN_COD_ORDERS = 5


def _money(value):
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def active_items(cart):
    return cart.items.filter(is_deleted=False).select_related("product", "variant")


@transaction.atomic
def refresh_prices(cart):
    """
    Bring open-cart lines up to the catalogue's current prices, so a shopper
    never starts a checkout on a stale price. Returns the lines that changed.
    """
    if cart.status != CART_OPEN:
        return []
    changed = []
    for item in active_items(cart):
        if item.variant is None:
            continue
        current = _money(item.variant.price)
        if current != item.unit_price:
            item.unit_price = current
            item.save(update_fields=["unit_price", "updated_at"])
            changed.append(item)
    return changed


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


@transaction.atomic
def merge_carts(source, target):
    """
    Fold `source` (usually a guest cart) into `target` (a customer's cart).
    Same product + variant adds up, capped at available stock; lines that are
    sold out are dropped rather than failing the merge. `source` is closed.
    Returns the target cart.
    """
    if source.pk == target.pk:
        return target
    # Lock in a fixed order so two concurrent merges can't deadlock
    locked = {c.pk: c for c in Cart.objects.select_for_update().filter(pk__in=[source.pk, target.pk]).order_by("pk")}
    source, target = locked[source.pk], locked[target.pk]

    if source.store_id != target.store_id:
        raise ValidationError("Carts belong to different stores.")
    if source.status != CART_OPEN or target.status != CART_OPEN:
        raise ValidationError("Only open carts can be merged.")

    now = timezone.now()
    for item in list(active_items(source)):
        in_stock = sellable(item.variant) if item.variant is not None else 0
        existing = CartItem.objects.filter(
            cart=target, product=item.product, variant=item.variant, is_deleted=False
        ).first()

        if existing:
            existing.quantity = min(existing.quantity + item.quantity, max(in_stock, existing.quantity))
            existing.save(update_fields=["quantity", "updated_at"])
        elif in_stock > 0:
            item.cart = target
            item.quantity = min(item.quantity, in_stock)
            item.save(update_fields=["cart", "quantity", "updated_at"])
            continue
        item.is_deleted, item.deleted_at = True, now
        item.save(update_fields=["is_deleted", "deleted_at", "updated_at"])

    source.status = CART_MERGED
    source.save(update_fields=["status", "updated_at"])
    return target


def claim_session_cart(store, session_id, customer):
    """A shopper signed in: their customer cart absorbs the guest cart from this session."""
    target = get_or_create_cart(store, customer=customer)
    guest = Cart.objects.filter(
        store=store, status=CART_OPEN, is_deleted=False, session_id=session_id, customer__isnull=True
    ).first()
    return merge_carts(guest, target) if guest else target


def _open_cart(cart):
    cart = Cart.objects.select_for_update().get(pk=cart.pk, is_deleted=False)
    if cart.status != CART_OPEN:
        raise ValidationError("This cart is no longer open.")
    return cart


def _check_stock(product, variant, quantity):
    """Quantity 0 (or no stock record) is sold out; never sell more than is available."""
    if variant is not None and variant.continue_selling:
        return  # the store keeps selling when out of stock
    in_stock = sellable(variant) if variant is not None else 0
    if in_stock <= 0:
        raise ValidationError({"product": f"{product.title} is sold out."})
    if quantity > in_stock:
        raise ValidationError({"quantity": f"Only {in_stock} of {product.title} left."})


def _check_available(store, product):
    """Same rule as the storefront: Active and on the Online Store."""
    if product.store_id != store.pk or not published_products(store).filter(pk=product.pk).exists():
        raise ValidationError({"product": f"{product.title} isn't available."})


@transaction.atomic
def add_item(cart, product, quantity, variant=None):
    """Add a product to the cart; adding it again raises the quantity."""
    cart = _open_cart(cart)

    if quantity < 1:
        raise ValidationError({"quantity": "Must be at least 1."})
    _check_available(cart.store, product)
    if variant is not None and variant.product_id != product.pk:
        raise ValidationError({"variant": f"Doesn't belong to {product.title}."})
    if variant is None:
        variant = product.variants.order_by("created_at").first()
    if variant is None:
        raise ValidationError({"product": "This product isn't available."})

    item = CartItem.objects.filter(cart=cart, product=product, variant=variant, is_deleted=False).first()
    _check_stock(product, variant, quantity + (item.quantity if item else 0))

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
        # Only asking for more needs stock: a shopper can always lower a line, even if stock fell below it since
        if quantity > item.quantity:
            _check_stock(item.product, item.variant, quantity)
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
def start_checkout(cart, email=None, shipping_address=None, billing_address=None, discount_code=None):
    """Freeze the cart's lines into a checkout. Reuses the cart's open checkout."""
    cart = _open_cart(cart)
    refresh_prices(cart)
    items = list(active_items(cart))

    if not items:
        raise ValidationError("The cart is empty.")
    # Stock may have run out since the items were added
    for item in items:
        _check_available(cart.store, item.product)
        _check_stock(item.product, item.variant, item.quantity)

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
    return update_checkout(
        checkout, email=email, shipping_address=shipping_address,
        billing_address=billing_address, discount_code=discount_code,
    )


def _price_discount(checkout, new_code):
    """Keep checkout.discount_amount in step with the subtotal and the applied code."""
    metadata = dict(checkout.metadata or {})
    code = metadata.get("discount_code") if new_code is None else new_code.strip()

    if not code:
        metadata.pop("discount_code", None)
        checkout.discount_amount = ZERO
    elif new_code is None:
        # Subtotal may have changed: re-price, drop the code if it no longer qualifies
        try:
            match = discounts.find_code(checkout.store, code)
            checkout.discount_amount = discounts.amount_for(match.discount, checkout.subtotal)
        except ValidationError:
            metadata.pop("discount_code", None)
            checkout.discount_amount = ZERO
    else:
        match = discounts.find_code(checkout.store, code)
        checkout.discount_amount = discounts.amount_for(match.discount, checkout.subtotal)
        metadata["discount_code"] = match.code

    checkout.metadata = metadata


def _tax_and_shipping(store, subtotal, discount_amount):
    """Tax on the discounted subtotal; flat shipping unless the free-shipping threshold is met."""
    taxable = max(subtotal - discount_amount, ZERO)
    tax = (taxable * Decimal(store.tax_rate or 0) / Decimal(100)).quantize(Decimal("0.01"), ROUND_HALF_UP)

    shipping = _money(store.shipping_flat_rate)
    threshold = store.free_shipping_threshold
    if subtotal <= ZERO or (threshold is not None and subtotal >= threshold):
        shipping = ZERO
    return tax, shipping


@transaction.atomic
def update_checkout(checkout, email=None, shipping_address=None, billing_address=None, discount_code=None):
    """Contact, addresses and discount code; totals are recomputed from the lines.

    discount_code: a code to apply, or "" to remove the applied one.
    """
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
    _price_discount(checkout, discount_code)
    checkout.tax_amount, checkout.shipping_amount = _tax_and_shipping(
        checkout.store, checkout.subtotal, checkout.discount_amount
    )
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


def _check_open_order_cap(customer):
    """Cash on delivery commits stock before any money moves: cap what one customer can hold open."""
    open_cod = Order.objects.filter(
        customer=customer, status="open", fulfillment_status="unfulfilled",
        payments__status="pending", payments__is_deleted=False,
    ).distinct().count()
    if open_cod >= MAX_OPEN_COD_ORDERS:
        raise ValidationError(
            f"You already have {open_cod} unpaid cash-on-delivery orders waiting. "
            "Please wait for them to be delivered or cancel one."
        )


def _guest_customer(checkout):
    """
    Guest checkout still leaves the store a customer record, matched by email.
    This only attributes the order; it never exposes the customer's existing
    cart or data to the session (email isn't verified).
    """
    customer = Customer.objects.filter(store=checkout.store, email__iexact=checkout.email).first()
    if customer is None:
        try:
            with transaction.atomic():
                customer = Customer.objects.create(
                    store=checkout.store, email=checkout.email, email_subscription_status="not_subscribed"
                )
        except IntegrityError:  # a concurrent checkout created it first
            customer = Customer.objects.get(store=checkout.store, email__iexact=checkout.email)
    return customer


@transaction.atomic
def reprice_checkout(checkout):
    """
    Bring an open checkout's lines, discount, tax and shipping up to date with
    the catalogue and store settings. Returns True if the total changed, so the
    shopper can confirm the new amount instead of being charged a different one.
    """
    checkout = Checkout.objects.select_for_update().get(pk=checkout.pk)
    if checkout.status != CHECKOUT_OPEN:
        return False

    before = checkout.total_amount
    for item in checkout.items.filter(is_deleted=False).select_related("variant"):
        if item.variant is None:
            continue
        current = _money(item.variant.price)
        if current != item.unit_price:
            item.unit_price = current
            item.total_price = current * item.quantity
            item.save(update_fields=["unit_price", "total_price", "updated_at"])
    return update_checkout(checkout).total_amount != before


def place_order(checkout, payment_method="cod"):
    """
    Turn an open checkout into an order. If prices, discount, tax or shipping
    moved since the shopper last saw the total, the checkout is refreshed and
    the order is refused so they can review the new total and confirm again.
    """
    if reprice_checkout(checkout):
        raise ValidationError({"total": "Prices or charges changed. Please review your order and confirm again."})
    return _place_order(checkout, payment_method)


@transaction.atomic
def _place_order(checkout, payment_method):
    """
    Commits stock, records the discount, closes the checkout and its cart.
    Safe to repeat: a completed checkout returns the order it already produced.

    payment_method: only "cod" (cash on delivery) for now; it leaves a pending
    payment for the order total, to be captured when the money arrives.
    """
    if payment_method not in PAYMENT_METHODS:
        raise ValidationError({"payment_method": f"Choose one of: {', '.join(PAYMENT_METHODS)}."})
    checkout = Checkout.objects.select_for_update().get(pk=checkout.pk)

    if checkout.status == CHECKOUT_COMPLETED:
        order = Order.objects.filter(pk=(checkout.metadata or {}).get("order_id")).first()
        if order is None:
            raise ValidationError("This checkout is already completed.")
        return order
    if checkout.status != CHECKOUT_OPEN:
        raise ValidationError("This checkout is no longer open.")

    if not checkout.email:
        raise ValidationError({"email": "An email is required to place the order."})
    if not checkout.shipping_address:
        raise ValidationError({"shipping_address": "A shipping address is required to place the order."})

    items = list(checkout.items.filter(is_deleted=False).select_related("product", "variant"))
    if not items:
        raise ValidationError("The checkout has no items.")

    # Catalogue prices may have moved since the cart was frozen
    for item in items:
        _check_available(checkout.store, item.product)
        _check_stock(item.product, item.variant, item.quantity)

    customer = checkout.customer or _guest_customer(checkout)
    _check_open_order_cap(customer)

    # Lock the discount so concurrent orders can't both take its last use
    code = (checkout.metadata or {}).get("discount_code")
    match = None
    if code and checkout.discount_amount > ZERO:
        match = discounts.find_code(checkout.store, code)
        Discount.objects.select_for_update().get(pk=match.discount_id)
        match = discounts.find_code(checkout.store, code)
        discounts.check_customer_limit(match.discount, customer)

    order = orders.create_order(
        checkout.store,
        [
            {"product": i.product, "variant": i.variant, "quantity": i.quantity, "unit_price": i.unit_price}
            for i in items
        ],
        customer=customer,
        currency=checkout.currency,
        discount_amount=checkout.discount_amount,
        tax_amount=checkout.tax_amount,
        shipping_amount=checkout.shipping_amount,
    )

    if match is not None:
        OrderDiscount.objects.create(
            order=order,
            discount=match.discount,
            discount_code=match,
            discount_amount=checkout.discount_amount,
            is_deleted=False,
        )

    payments.record_pending_payment(
        checkout.store, order, checkout, order.total, provider=payment_method, customer=customer
    )

    checkout.customer = customer
    checkout.metadata = {**(checkout.metadata or {}), "order_id": str(order.pk), "payment_method": payment_method}
    checkout.save(update_fields=["customer", "metadata", "updated_at"])
    complete_checkout(checkout)
    return order


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


def expire_carts(store, idle_for):
    """
    Close open carts untouched for `idle_for` (a timedelta) that have no open
    checkout in progress. Their lines stay for reporting. Returns how many.
    """
    cutoff = timezone.now() - idle_for
    in_checkout = Checkout.objects.filter(status=CHECKOUT_OPEN, is_deleted=False).values("cart_id")
    return (
        Cart.objects.filter(store=store, status=CART_OPEN, is_deleted=False, updated_at__lt=cutoff)
        .exclude(pk__in=in_checkout)
        .update(status=CART_EXPIRED, updated_at=timezone.now())
    )
