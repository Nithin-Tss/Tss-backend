"""
Carts and checkouts. A shopper fills a cart, then starts a checkout, which
freezes the cart's lines and prices. Turning a checkout into an order is the
orders app's job (orders.services.create_order).
"""
import re
from decimal import ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction
from django.dispatch import Signal
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.catalog.services import published_products
from apps.discounts import services as discounts
from apps.discounts.models import Discount, DiscountCode, OrderDiscount
from apps.customers.models import Customer
from apps.inventory.services import sellable, sellable_for, sells_without_stock, convert_reservations
from apps.orders import services as orders
from apps.orders.models import Order
from apps.payments import services as payments

from . import tax as tax_rules
from .models import Cart, CartItem, Checkout, CheckoutItem

CART_OPEN, CART_CHECKED_OUT, CART_MERGED, CART_EXPIRED = "open", "checked_out", "merged", "expired"
checkout_abandoned = Signal()  # sender=Checkout, checkout=...

CHECKOUT_OPEN, CHECKOUT_COMPLETED, CHECKOUT_ABANDONED = "open", "completed", "abandoned"

ZERO = Decimal("0.00")
PAYMENT_METHODS = ("cod", "gift_card")  # what a shopper may pick at /complete/
ONLINE_METHODS = ("stripe",)  # placed only by the payment provider once money is confirmed
MAX_OPEN_COD_ORDERS = 5
MAX_LINE_QUANTITY = 999  # per cart line: stops absurd orders, even where the store sells past stock


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


def get_or_create_cart(store, session_id=None, customer=None, currency=None):
    """The shopper's open cart: by customer if signed in, otherwise by session. In the store's currency."""
    if customer is None and not session_id:
        raise ValidationError({"session_id": "A session id or customer is required."})

    carts = Cart.objects.filter(store=store, status=CART_OPEN, is_deleted=False)
    cart = carts.filter(customer=customer).first() if customer else carts.filter(session_id=session_id).first()

    if cart is None:
        cart = Cart.objects.create(
            store=store,
            customer=customer,
            session_id=session_id,
            currency=currency or store.currency,
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
    lines = list(active_items(source))
    # What is still for sale on the store (Active and on the Online Store): the rest is not carried over
    for_sale = set(
        published_products(target.store).prefetch_related(None)
        .filter(pk__in={i.product_id for i in lines}).values_list("pk", flat=True)
    )
    for item in lines:
        # A variant that keeps selling past its stock is not capped by it (only by the per-line limit)
        sells_on = item.variant is not None and sells_without_stock(item.variant)
        in_stock = sellable(item.variant) if item.variant is not None else 0
        existing = CartItem.objects.filter(
            cart=target, product=item.product, variant=item.variant, is_deleted=False
        ).first()

        if item.product_id not in for_sale:
            pass
        elif existing:
            cap = MAX_LINE_QUANTITY if sells_on else max(in_stock, existing.quantity)
            existing.quantity = min(existing.quantity + item.quantity, cap)
            existing.save(update_fields=["quantity", "updated_at"])
        elif sells_on or in_stock > 0:
            item.cart = target
            item.quantity = min(item.quantity, MAX_LINE_QUANTITY if sells_on else in_stock)
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


def _check_stock(product, variant, quantity, checkout_id=None):
    """
    Tracked stock at 0 is sold out; never sell more than is available. With a checkout id,
    stock held for OTHER checkouts doesn't count as available (this checkout's own hold does).
    A variant that isn't tracked (no inventory rows) or keeps selling is never limited by stock.
    """
    if variant is not None and sells_without_stock(variant):
        return
    in_stock = sellable_for(variant, checkout_id) if variant is not None else 0
    if in_stock <= 0:
        raise ValidationError({"product": f"{product.title} is sold out."})
    if quantity > in_stock:
        raise ValidationError({"quantity": f"Only {in_stock} of {product.title} left."})


def _check_line_limit(product, quantity):
    if quantity > MAX_LINE_QUANTITY:
        raise ValidationError({"quantity": f"You can buy at most {MAX_LINE_QUANTITY} of {product.title} at a time."})


def _check_available(store, product):
    """Same rule as the storefront: Active and on the Online Store."""
    if product.store_id != store.pk or not published_products(store).filter(pk=product.pk).exists():
        raise ValidationError({"product": f"{product.title} isn't available."})


@transaction.atomic
def add_item(cart, product, quantity, variant=None, metadata=None):
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

    # A gift card is bought one at a time: each has its own recipient, so adding it again is a
    # second, separate card rather than raising a shared line's quantity.
    is_gift_card = product.product_type == "gift_card"
    if is_gift_card:
        quantity = 1
        item = None
    else:
        item = CartItem.objects.filter(cart=cart, product=product, variant=variant, is_deleted=False).first()
    wanted = quantity + (item.quantity if item else 0)
    _check_line_limit(product, wanted)
    _check_stock(product, variant, wanted)

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
            metadata=metadata or None,
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
            _check_line_limit(item.product, quantity)
            _check_stock(item.product, item.variant, quantity)
        item.quantity = quantity
        item.save(update_fields=["quantity", "updated_at"])
    return item


@transaction.atomic
def clear_cart(cart):
    cart = _open_cart(cart)
    cart.items.filter(is_deleted=False).update(is_deleted=True, deleted_at=timezone.now())
    return cart


EMAIL_MAX_LENGTH = 254
_EMAIL_PATTERN = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*\.[A-Za-z]{2,}$"
)


def clean_email(value):
    """The shopper's email trimmed and lower-cased; ValidationError({"email": ...}) when missing or not name@domain.tld."""
    value = (value or "").strip().lower() if isinstance(value, str) or value is None else ""
    if not value:
        raise ValidationError({"email": "Enter an email address"})
    if len(value) > EMAIL_MAX_LENGTH or not _EMAIL_PATTERN.match(value):
        raise ValidationError({"email": "Enter a valid email address"})
    return value


def cart_subtotal(cart):
    return sum((i.unit_price * i.quantity for i in active_items(cart)), ZERO)


@transaction.atomic
def start_checkout(cart, email=None, shipping_address=None, billing_address=None, discount_code=None, gift_card_code=None):
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

    if cart.currency != cart.store.currency:
        cart.currency = cart.store.currency
        cart.save(update_fields=["currency", "updated_at"])

    checkout = Checkout.objects.filter(cart=cart, status=CHECKOUT_OPEN, is_deleted=False).first()

    if checkout is None:
        checkout = Checkout.objects.create(
            store=cart.store,
            cart=cart,
            customer=cart.customer,
            currency=cart.store.currency,
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
            metadata=item.metadata,
            is_deleted=False,
        )
        subtotal += line_total

    checkout.subtotal = subtotal
    checkout.total_amount = subtotal
    checkout.abandoned_at = None
    return update_checkout(
        checkout, email=email, shipping_address=shipping_address,
        billing_address=billing_address, discount_code=discount_code, gift_card_code=gift_card_code,
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


def _price_gift_card(checkout, new_code):
    """
    Keep checkout.gift_card_amount in step with the applied card's live balance and the order
    total (set just before this runs). Only one gift card may be applied at a time; applying a
    new code replaces whichever one was there. "" removes it.
    """
    from apps.gift_cards import services as gift_cards

    metadata = dict(checkout.metadata or {})
    code = metadata.get("gift_card_code") if new_code is None else (new_code or "").strip()

    def clear():
        metadata.pop("gift_card_code", None)
        metadata.pop("gift_card_id", None)
        checkout.gift_card_amount = ZERO

    if not code:
        clear()
        checkout.metadata = metadata
        return

    card = gift_cards.get_by_code(checkout.store, code)
    problem = None
    if card is None:
        problem = "We couldn't find a gift card with that code."
    elif card.status != gift_cards.ACTIVE:
        problem = "This gift card is disabled."
    elif card.expires_at and card.expires_at <= timezone.now():
        problem = "This gift card has expired."
    elif card.currency_code != checkout.currency:
        problem = f"This gift card is in {card.currency_code}; this order is in {checkout.currency}."
    elif card.current_balance <= ZERO:
        problem = "This gift card has no balance left."

    if problem:
        if new_code is None:
            # Re-pricing an existing checkout: a code that no longer qualifies is dropped, not an error
            clear()
            checkout.metadata = metadata
            return
        raise ValidationError({"gift_card_code": problem})

    checkout.gift_card_amount = min(_money(card.current_balance), checkout.total_amount)
    metadata["gift_card_code"] = code.strip().upper()
    metadata["gift_card_id"] = str(card.pk)
    checkout.metadata = metadata


def amount_due(checkout):
    """What is actually charged online or taken cash-on-delivery, after a redeemed gift card."""
    return max(checkout.total_amount - (checkout.gift_card_amount or ZERO), ZERO)


def gift_card_last4(checkout):
    """The last 4 characters of the applied gift card, for "Gift card •••• 1234"; None if none applied."""
    gift_card_id = (checkout.metadata or {}).get("gift_card_id")
    if not gift_card_id:
        return None
    from apps.gift_cards.models import GiftCard

    card = GiftCard.objects.filter(pk=gift_card_id, store=checkout.store).first()
    return card.code_last4 if card else None


def resolve_checkout_code(store, code):
    """
    The checkout's single "Discount code or gift card" field: which kind is this, so the caller
    applies it the right way (discount_code= or gift_card_code= on start_checkout/update_checkout).
    Returns "discount", "gift_card", or None for a blank code. A code matching neither raises one
    generic error -- never says which kind was expected, same as Shopify.
    """
    from apps.gift_cards import services as gift_cards

    code = (code or "").strip()
    if not code:
        return None
    if DiscountCode.objects.filter(discount__store=store, code__iexact=code, is_deleted=False).exists():
        return "discount"
    if gift_cards.get_by_code(store, code) is not None:
        return "gift_card"
    raise ValidationError({"code": "This code isn't valid."})


def _shipping_rate(store, country, weight, region=None):
    """
    Fee of the best ShippingRate for this destination and cart weight, or None
    if the store has none that fit. A zone rate covering the shopper's region
    beats a country rate (its own country, or a zone covering the whole
    country), which beats an anywhere rate; then the band with the highest
    minimum weight wins.
    """
    country = (country or "").strip().lower()
    region = (region or "").strip().lower()
    # zone id -> how closely it covers the destination: 2 its region, 1 the whole country
    covers = {}
    for loc in store.shipping_zone_locations.all():
        loc_region = loc.region.strip().lower()
        if loc.country.strip().lower() != country or (loc_region and loc_region != region):
            continue
        covers[loc.zone_id] = max(covers.get(loc.zone_id, 0), 2 if loc_region else 1)
    best = None
    for rate in store.shipping_rates.all():
        if rate.zone_id is not None:
            if rate.zone_id not in covers:
                continue
            fit = covers[rate.zone_id]
        else:
            rate_country = rate.country.strip().lower()
            if rate_country and rate_country != country:
                continue
            fit = 1 if rate_country else 0
        if weight < rate.min_weight or (rate.max_weight is not None and weight >= rate.max_weight):
            continue
        key = (fit, rate.min_weight)
        if best is None or key > best[0]:
            best = (key, rate)
    return None if best is None else _money(best[1].price)


def _tax_and_shipping(store, subtotal, discount_amount, country=None, weight=ZERO, region=None, lines=None, exempt=False):
    """
    Tax by the store's tax rules (see tax.py), on the discounted lines. Shipping
    is the matching weight/country rate, else the flat fee; free once the
    discounted subtotal meets the threshold.

    lines: [{"amount", "taxable"}]; without them the subtotal is one taxable line.
    """
    taxable = max(subtotal - discount_amount, ZERO)

    shipping = _shipping_rate(store, country, weight, region)
    if shipping is None:
        shipping = _money(store.shipping_flat_rate)
    threshold = store.free_shipping_threshold
    if subtotal <= ZERO or (threshold is not None and taxable >= threshold):
        shipping = ZERO

    result = tax_rules.calculate(
        store, lines if lines is not None else [{"amount": subtotal, "taxable": True}],
        discount=discount_amount, shipping=shipping, country=country, region=region, exempt=exempt,
    )
    return result.total, shipping


def _cart_weight(items):
    return sum(((i.variant.weight or 0) * i.quantity for i in items if i.variant is not None), ZERO)


@transaction.atomic
def update_checkout(
    checkout, email=None, shipping_address=None, billing_address=None, discount_code=None, gift_card_code=None,
):
    """Contact, addresses, discount code and gift card code; totals are recomputed from the lines.

    discount_code / gift_card_code: a code to apply, or "" to remove the applied one.
    """
    checkout = Checkout.objects.select_for_update().get(pk=checkout.pk)

    if checkout.status != CHECKOUT_OPEN:
        raise ValidationError("This checkout is no longer open.")

    if email is not None:
        checkout.email = clean_email(email) if str(email).strip() else ""
    if shipping_address is not None:
        checkout.shipping_address = shipping_address
    if billing_address is not None:
        checkout.billing_address = billing_address

    checkout.currency = checkout.store.currency  # what the order and the card payment are made in
    checkout.subtotal = sum((i.total_price for i in checkout.items.filter(is_deleted=False)), ZERO)
    _price_discount(checkout, discount_code)
    lines = list(checkout.items.filter(is_deleted=False).select_related("variant", "product"))
    address = checkout.shipping_address if isinstance(checkout.shipping_address, dict) else {}
    checkout.tax_amount, checkout.shipping_amount = _tax_and_shipping(
        checkout.store, checkout.subtotal, checkout.discount_amount,
        country=address.get("country"), weight=_cart_weight(lines),
        region=address.get("state") or address.get("region") or address.get("province"),
        lines=[{"amount": i.total_price, "taxable": i.product.is_taxable} for i in lines],
        exempt=bool(checkout.customer and checkout.customer.tax_exempt),
    )
    checkout.total_amount = tax_rules.grand_total(
        checkout.store, checkout.subtotal, checkout.discount_amount, checkout.tax_amount, checkout.shipping_amount
    )
    _price_gift_card(checkout, gift_card_code)
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
def _place_order(checkout, payment_method, payment_reference=None):
    """
    Commits stock, records the discount, closes the checkout and its cart.
    Safe to repeat: a completed checkout returns the order it already produced.

    payment_method: "cod" (cash on delivery) leaves a pending payment for the order
    total, to be captured when the money arrives. An online method ("stripe") is
    only passed once the provider confirmed the money, with its reference, and
    records the payment as captured.
    """
    if payment_method not in PAYMENT_METHODS + ONLINE_METHODS:
        raise ValidationError({"payment_method": f"Choose one of: {', '.join(PAYMENT_METHODS)}."})
    online = payment_method in ONLINE_METHODS
    checkout = Checkout.objects.select_for_update().get(pk=checkout.pk)

    if checkout.status == CHECKOUT_COMPLETED:
        order = Order.objects.filter(pk=(checkout.metadata or {}).get("order_id")).first()
        if order is None:
            raise ValidationError("This checkout is already completed.")
        return order
    if checkout.status != CHECKOUT_OPEN:
        raise ValidationError("This checkout is no longer open.")

    # Every path that makes an order (cash, card, Stripe) ends here, so an email saved before the rules existed can't slip through
    checkout.email = clean_email(checkout.email)
    if not checkout.shipping_address:
        raise ValidationError({"shipping_address": "A shipping address is required to place the order."})

    items = list(checkout.items.filter(is_deleted=False).select_related("product", "variant"))
    if not items:
        raise ValidationError("The checkout has no items.")

    # Catalogue prices may have moved since the cart was frozen
    for item in items:
        _check_available(checkout.store, item.product)
        _check_stock(item.product, item.variant, item.quantity, checkout.pk)

    sells_gift_card = any(i.product.product_type == "gift_card" for i in items)
    if checkout.gift_card_amount and sells_gift_card:
        raise ValidationError({"gift_card_code": "A gift card can't be used to buy a gift card. Remove it to continue."})
    if sells_gift_card and payment_method == "cod":
        raise ValidationError({"payment_method": "A gift card can only be paid for online, not cash on delivery."})

    customer = checkout.customer or _guest_customer(checkout)
    if payment_method == "cod":
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
            {
                "product": i.product, "variant": i.variant, "quantity": i.quantity,
                "unit_price": i.unit_price, "metadata": i.metadata,
            }
            for i in items
        ],
        customer=customer,
        currency=checkout.currency,
        discount_amount=checkout.discount_amount,
        tax_amount=checkout.tax_amount,
        shipping_amount=checkout.shipping_amount,
        checkout=checkout,
    )
    if order.total != checkout.total_amount:
        # What was shown, and what a card was charged, is the checkout's total: never place an order for another one
        raise ValidationError({"total": "Prices or charges changed. Please review your order and confirm again."})

    if match is not None:
        OrderDiscount.objects.create(
            order=order,
            discount=match.discount,
            discount_code=match,
            discount_amount=checkout.discount_amount,
            is_deleted=False,
        )

    # A redeemed gift card is debited here, once the order has committed to its final total, and
    # pays for its share before anything else does.
    gift_card_amount = checkout.gift_card_amount or ZERO
    due = max(order.total - gift_card_amount, ZERO)
    if payment_method == "gift_card" and due > ZERO:
        raise ValidationError({"payment_method": f"The gift card doesn't cover the full total; {due} is still due."})

    if gift_card_amount > ZERO:
        from apps.gift_cards import services as gift_cards
        from apps.gift_cards.models import GiftCard

        card = GiftCard.objects.select_for_update().get(
            pk=(checkout.metadata or {}).get("gift_card_id"), store=checkout.store
        )
        if card.currency_code != checkout.currency:
            raise ValidationError({"gift_card_code": "This gift card's currency no longer matches this order."})
        # The Payment comes first so the debit can point back at it: a refund of this Payment
        # looks up its gift card debits by this link (see apps.payments.services.refund).
        gift_card_payment = payments.record_payment(checkout.store, order, gift_card_amount, provider="gift_card")
        gift_cards.change_balance(
            card, -gift_card_amount, kind="debit", order=order, payment=gift_card_payment, reason="Redeemed at checkout"
        )

    if online:
        if due > ZERO:
            payments.record_payment(checkout.store, order, due, provider=payment_method, reference=payment_reference)
    elif due > ZERO:
        payments.record_pending_payment(
            checkout.store, order, checkout, due, provider=payment_method, customer=customer
        )

    if sells_gift_card:
        from apps.gift_cards import purchases as gift_card_purchases

        for order_item in order.items.filter(product__product_type="gift_card"):
            gift_card_purchases.issue_from_order_item(order_item)

    checkout.customer = customer
    checkout.metadata = {**(checkout.metadata or {}), "order_id": str(order.pk), "payment_method": payment_method}
    checkout.save(update_fields=["customer", "metadata", "updated_at"])
    complete_checkout(checkout)
    convert_reservations(checkout.pk)
    orders.mark_cart_event(checkout.store, checkout.cart.session_id, order=order)  # the visit that ordered
    return order


def mark_abandoned(store, idle_for):
    """Flag open checkouts untouched for `idle_for` (a timedelta). Returns how many."""
    now = timezone.now()
    stale = Checkout.objects.filter(
        store=store,
        status=CHECKOUT_OPEN,
        is_deleted=False,
        abandoned_at__isnull=True,
        updated_at__lt=now - idle_for,
    )
    with transaction.atomic():
        flagged = list(stale.select_for_update())
        Checkout.objects.filter(pk__in=[c.pk for c in flagged]).update(abandoned_at=now, recovery_status="pending")
        for checkout in flagged:
            transaction.on_commit(lambda checkout=checkout: checkout_abandoned.send(sender=Checkout, checkout=checkout))
    return len(flagged)


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
