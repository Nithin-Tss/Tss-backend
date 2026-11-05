"""
Stripe, with each store's own API keys. The owner enters their publishable and secret keys; we validate them
with Stripe, then shoppers pay the store's own Stripe account directly, so the money, the dispute liability
and the Stripe dashboard all belong to the store. Secret keys are encrypted at rest.

An order is only created once Stripe confirms the money (payment_intent.succeeded,
via the webhook or the shopper's return from Stripe, whichever comes first), so
unpaid checkouts never hold stock. If the order cannot be placed after the shopper
paid (stock ran out, the total moved), the payment is refunded straight away.
"""
import logging
import re
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

import stripe
from django.conf import settings
from django.db import transaction
from rest_framework.exceptions import ValidationError

from apps.commerce import services as commerce
from apps.inventory import services as inventory
from apps.commerce.models import Checkout
from apps.tenancy.models import Store

from . import crypto
from . import services as payments
from .models import Payment, PaymentTransaction

logger = logging.getLogger(__name__)

PROVIDER = "stripe"
STOCK_HOLD = timedelta(minutes=getattr(settings, "STOCK_HOLD_MINUTES", 15))
SUCCEEDED = "succeeded"
DISPUTE = "dispute"
# Currencies Stripe counts in whole units, not 1/100ths
ZERO_DECIMAL = {
    "BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA", "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF",
}


def plain(obj):
    """
    Events and intents from the real Stripe library are Stripe objects, not dicts: they have no
    .get(). Handlers that read them dict-style go through this first (a plain dict passes through).
    """
    return obj.to_dict() if hasattr(obj, "to_dict") else (obj or {})


def client(store=None):
    """A Stripe client using the store's own secret key (or, with no store, the platform's)."""
    if store is not None:
        key = crypto.decrypt(store.stripe_secret_key_enc)
        if not key:
            raise ValidationError("This store can't take card payments yet.")
        return stripe.StripeClient(key)
    if not settings.STRIPE_SECRET_KEY:
        raise ValidationError("Card payments are not configured on this server.")
    return stripe.StripeClient(settings.STRIPE_SECRET_KEY)


def from_minor_units(amount, currency):
    amount = Decimal(amount)
    return amount if currency.upper() in ZERO_DECIMAL else amount / 100


def to_minor_units(amount, currency):
    amount = Decimal(amount)
    # Rounded, never truncated: a stray fraction must not charge less than the order total
    return int(amount.to_integral_value(ROUND_HALF_UP)) if currency.upper() in ZERO_DECIMAL else int((amount * 100).to_integral_value(ROUND_HALF_UP))


def _require_connected(store):
    if not store.stripe_account_id or not store.stripe_charges_enabled:
        raise ValidationError("This store can't take card payments yet.")


# -- Connecting a store ------------------------------------------------------------------------

KEY_PATTERNS = {
    "publishable_key": re.compile(r"^pk_(test|live)_[A-Za-z0-9]{10,}$"),
    "secret_key": re.compile(r"^(?:sk|rk)_(test|live)_[A-Za-z0-9]{10,}$"),
}
WEBHOOK_EVENTS = [
    "payment_intent.succeeded", "payment_intent.payment_failed", "payment_intent.canceled",
    "charge.refunded", "charge.dispute.created", "charge.dispute.closed",
]


def webhook_url(store):
    base = getattr(settings, "PUBLIC_API_URL", "")
    return f"{base}/api/v1/payments/stripe/webhook/{store.pk}/" if base else ""


def connect_with_keys(store, publishable_key, secret_key, webhook_secret=""):
    """
    Validate the keys the owner typed in, then connect the store. Checks the format of each key, that both are for
    the same mode (test or live), and that Stripe accepts the secret key (by fetching the account it belongs to).
    Nothing is saved unless every check passes.
    """
    publishable_key, secret_key, webhook_secret = (publishable_key or "").strip(), (secret_key or "").strip(), (webhook_secret or "").strip()
    errors = {}
    pk_match = KEY_PATTERNS["publishable_key"].match(publishable_key)
    sk_match = KEY_PATTERNS["secret_key"].match(secret_key)
    if not pk_match:
        errors["publishable_key"] = "Enter your publishable key (it starts with pk_test_ or pk_live_)."
    if not sk_match:
        errors["secret_key"] = "Enter your secret key (it starts with sk_test_ or sk_live_)."
    if webhook_secret and not webhook_secret.startswith("whsec_"):
        errors["webhook_secret"] = "A webhook signing secret starts with whsec_."
    if errors:
        raise ValidationError(errors)
    if pk_match.group(1) != sk_match.group(1):
        raise ValidationError({"secret_key": "The publishable and secret keys are for different modes (one test, one live)."})
    livemode = sk_match.group(1) == "live"

    try:
        account = stripe.StripeClient(secret_key).v1.accounts.retrieve()
    except stripe.AuthenticationError:
        raise ValidationError({"secret_key": "Stripe rejected this secret key. Check it and try again."})
    except stripe.PermissionError:
        raise ValidationError({"secret_key": "This key can't read the account. Use a secret key, or give a restricted key read access to Accounts."})
    if Store.objects.filter(stripe_account_id=account.id).exclude(pk=store.pk).exists():
        raise ValidationError({"secret_key": "This Stripe account is already connected to another store."})

    endpoint_id = store.stripe_webhook_endpoint_id if store.stripe_account_id == account.id else ""
    url = webhook_url(store)
    if not webhook_secret and url and not endpoint_id:
        # Register the webhook for the owner; Stripe returns its signing secret only at creation
        try:
            endpoint = stripe.StripeClient(secret_key).v1.webhook_endpoints.create(
                {"url": url, "enabled_events": WEBHOOK_EVENTS, "description": "Store payments"}
            )
            webhook_secret, endpoint_id = endpoint.secret, endpoint.id
        except stripe.StripeError as exc:
            logger.warning("Could not register Stripe webhook for store %s: %s", store.pk, exc)

    store.stripe_account_id = account.id
    store.stripe_charges_enabled = bool(account.charges_enabled)
    store.stripe_details_submitted = bool(account.details_submitted)
    store.stripe_publishable_key = publishable_key
    store.stripe_secret_key_enc = crypto.encrypt(secret_key)
    if webhook_secret:
        store.stripe_webhook_secret_enc = crypto.encrypt(webhook_secret)
    store.stripe_webhook_endpoint_id = endpoint_id
    store.stripe_livemode = livemode
    store.save(update_fields=[
        "stripe_account_id", "stripe_charges_enabled", "stripe_details_submitted", "stripe_publishable_key",
        "stripe_secret_key_enc", "stripe_webhook_secret_enc", "stripe_webhook_endpoint_id", "stripe_livemode", "updated_at",
    ])
    return store


def sync_account(store, account=None):
    """Refresh whether the store may take payments (account.updated, or when the page is opened)."""
    if not store.stripe_account_id:
        return store
    if account is None:
        try:
            account = client(store).v1.accounts.retrieve()
        except stripe.StripeError:
            logger.warning("Could not refresh Stripe account for store %s", store.pk)
            return store
    store.stripe_charges_enabled = bool(account.charges_enabled)
    store.stripe_details_submitted = bool(account.details_submitted)
    store.save(update_fields=["stripe_charges_enabled", "stripe_details_submitted", "updated_at"])
    return store


def clear_connection(store, remove_webhook=True):
    """Forget the store's keys and stop taking card payments. Removes the webhook we registered, if any."""
    if remove_webhook and store.stripe_webhook_endpoint_id and store.stripe_secret_key_enc:
        try:
            client(store).v1.webhook_endpoints.delete(store.stripe_webhook_endpoint_id)
        except Exception:
            logger.warning("Could not delete Stripe webhook %s for store %s", store.stripe_webhook_endpoint_id, store.pk)
    for field in ("stripe_account_id", "stripe_publishable_key", "stripe_secret_key_enc", "stripe_webhook_secret_enc", "stripe_webhook_endpoint_id"):
        setattr(store, field, "")
    store.stripe_charges_enabled = store.stripe_details_submitted = store.stripe_livemode = False
    store.save()
    return store


# -- Taking a payment --------------------------------------------------------------------------

def payment_session(checkout):
    """
    The PaymentIntent for this checkout's current total, for the shopper's Payment Element.
    Reused while the total is unchanged, updated if it moved, and never reused once paid.
    """
    _require_connected(checkout.store)
    if commerce.reprice_checkout(checkout):
        raise ValidationError({"total": "Prices or charges changed. Please review your order and confirm again."})
    checkout.refresh_from_db()
    if checkout.status != commerce.CHECKOUT_OPEN:
        raise ValidationError("This checkout is no longer open.")
    if not checkout.email or not checkout.shipping_address:
        raise ValidationError("Add your email and shipping address before paying.")
    if checkout.total_amount <= 0:
        raise ValidationError("There is nothing to pay for.")

    items = list(checkout.items.filter(is_deleted=False).select_related("product", "variant"))
    for item in items:
        commerce._check_available(checkout.store, item.product)  # still Active and on the Online Store
        commerce._check_stock(item.product, item.variant, item.quantity, checkout.pk)
    # Hold the stock while the shopper pays; a hold that runs out is simply re-taken if stock is still there
    inventory.reserve(checkout.pk, checkout.store_id, [(i.variant, i.quantity) for i in items], STOCK_HOLD)

    store = checkout.store
    options = {}
    amount = to_minor_units(checkout.total_amount, checkout.currency)
    intents = client(store).v1.payment_intents
    intent_id = (checkout.metadata or {}).get("stripe_payment_intent")

    intent = None
    if intent_id:
        intent = intents.retrieve(intent_id, options=options)
        if intent.status == SUCCEEDED:
            raise ValidationError("This checkout has already been paid.")
        if intent.status == "processing":
            # A payment is in flight: its amount can't change, and a second attempt could double charge
            raise ValidationError("Your payment is still being processed. Please wait a moment.")
        if intent.status == "canceled" or intent.currency != checkout.currency.lower():
            intent = None
        elif intent.amount != amount:
            intent = intents.update(intent.id, {"amount": amount}, options=options)

    if intent is None:
        intent = intents.create(
            {
                "amount": amount,
                "currency": checkout.currency.lower(),
                "automatic_payment_methods": {"enabled": True},
                "receipt_email": checkout.email,
                "metadata": {"checkout_id": str(checkout.pk), "store_id": str(store.pk)},
            },
            options={**options, "idempotency_key": f"checkout-{checkout.pk}-{amount}-{intent_id or 'new'}"},
        )
        checkout.metadata = {**(checkout.metadata or {}), "stripe_payment_intent": intent.id}
        checkout.save(update_fields=["metadata", "updated_at"])

    return {
        "client_secret": intent.client_secret,
        "publishable_key": store.stripe_publishable_key,
        "stripe_account": None,  # the store's own keys: charges are made directly on its account
        "amount": checkout.total_amount,
        "currency": checkout.currency,
    }


def finalize(intent, account_id):
    """
    A PaymentIntent was paid: place the order for its checkout. Safe to call many times
    (webhook retries, webhook racing the shopper's return): the same order comes back.
    Returns the order, or None if the money was refunded instead.
    """
    if intent.status != SUCCEEDED:
        return None
    checkout_id = plain(intent.metadata).get("checkout_id")
    checkout = (
        Checkout.objects.select_related("store")
        .filter(pk=checkout_id, store__stripe_account_id=account_id, is_deleted=False)
        .first()
        if checkout_id
        else None
    )
    if checkout is None:
        logger.error("Stripe payment %s matches no checkout on account %s", intent.id, account_id)
        return None

    try:
        with transaction.atomic():
            checkout = Checkout.objects.select_for_update().get(pk=checkout.pk)
            if checkout.status == commerce.CHECKOUT_OPEN and (
                intent.amount != to_minor_units(checkout.total_amount, checkout.currency)
                or intent.currency != checkout.currency.lower()
            ):
                raise ValidationError("The amount paid doesn't match the order total.")
            order = commerce._place_order(checkout, PROVIDER, payment_reference=intent.id)
            # The checkout was already completed by some other payment: this money has no order
            if not order.payments.filter(provider=PROVIDER, provider_payment_id=intent.id).exists():
                raise ValidationError("This checkout was already paid another way.")
            return order
    except ValidationError as exc:
        logger.warning("Refunding Stripe payment %s, order not placed: %s", intent.id, exc.detail)
        why = str(exc.detail)[:300]
        try:
            refund = refund_intent(intent.id, checkout.store, reason="order_not_placed")
        except Exception as failure:
            # Raised on so Stripe retries the webhook (the refund key makes that safe), but say loudly that money is unplaced
            logger.exception("REFUND FAILED for Stripe payment %s on account %s: no order and money not returned", intent.id, account_id)
            _note_auto_refund(checkout.pk, intent, "failed", why, error=str(failure)[:300])
            raise
        _note_auto_refund(checkout.pk, intent, "refunded", why, refund_id=getattr(refund, "id", None))
        return None


def _note_auto_refund(checkout_id, intent, state, why, refund_id=None, error=None):
    """
    Keep a visible record, on the checkout, of money taken for an order that could not be placed:
    Checkout.metadata["auto_refunds"], one entry per payment (a retry updates it). Staff read it from the
    checkouts API; a "failed" entry means money is held with no order and needs someone's attention.
    Never allowed to hide the real outcome: a problem in here is only logged.
    """
    from django.utils import timezone

    try:
        with transaction.atomic():
            checkout = Checkout.objects.select_for_update().get(pk=checkout_id)
            entries = [e for e in (checkout.metadata or {}).get("auto_refunds", []) if e.get("payment_intent") != intent.id]
            before = next((e for e in (checkout.metadata or {}).get("auto_refunds", []) if e.get("payment_intent") == intent.id), {})
            entries.append({
                "payment_intent": intent.id,
                "amount": str(Decimal(from_minor_units(intent.amount, intent.currency)).quantize(Decimal("0.01"))),
                "currency": str(intent.currency).upper(),
                "status": state,
                "reason": why,
                "refund_id": refund_id,
                "error": error,
                "attempts": before.get("attempts", 0) + 1,
                "at": timezone.now().isoformat(),
            })
            checkout.metadata = {**(checkout.metadata or {}), "auto_refunds": entries}
            checkout.save(update_fields=["metadata", "updated_at"])
    except Exception:
        logger.exception("Could not record the automatic refund of Stripe payment %s on checkout %s", intent.id, checkout_id)


def refund_intent(intent_id, store, amount=None, reason=None):
    params = {"payment_intent": intent_id}
    if amount is not None:
        params["amount"] = amount
    if reason == "order_not_placed":
        params["metadata"] = {"reason": reason}
    key = f"refund-{intent_id}-{amount or 'full'}"
    return client(store).v1.refunds.create(params, options={"idempotency_key": key})


def refund_payment(payment, amount, already_refunded):
    """Refund part or all of a stripe Payment on the store's account; returns the Stripe refund id."""
    account_id = payment.store.stripe_account_id
    if not account_id:
        raise ValidationError("This store is no longer connected to Stripe.")
    minor = to_minor_units(amount, payment.currency)
    # The same refund asked twice must not repeat: the key names this payment and how much is already refunded
    key = f"refund-{payment.pk}-{already_refunded}-{minor}"
    try:
        refund = client(payment.store).v1.refunds.create(
            {"payment_intent": payment.provider_payment_id, "amount": minor},
            options={"idempotency_key": key},
        )
    except stripe.StripeError as exc:
        raise ValidationError({"payment": f"Stripe refused the refund: {exc.user_message or exc}"})
    return refund.id


# -- Events that arrive on their own -----------------------------------------------------------

def _stripe_payment(intent_id, account_id):
    if not intent_id or not account_id:
        return None
    return Payment.objects.filter(
        provider=PROVIDER, provider_payment_id=intent_id, store__stripe_account_id=account_id, is_deleted=False
    ).first()


def record_charge_refunds(charge, account_id):
    """charge.refunded: a refund made outside the app (Stripe dashboard) is recorded here too."""
    charge = plain(charge)
    payment = _stripe_payment(charge.get("payment_intent"), account_id)
    if payment is None:
        # Includes the automatic "order_not_placed" refunds: those payments never became orders
        return None
    total = from_minor_units(charge.get("amount_refunded") or 0, charge.get("currency") or payment.currency)
    return payments.record_external_refund(payment, total, reason="refunded_in_stripe")


def record_dispute(dispute, account_id):
    """charge.dispute.created / .closed: keep an audit row and the latest state on the payment."""
    dispute = plain(dispute)
    payment = _stripe_payment(dispute.get("payment_intent"), account_id)
    if payment is None:
        logger.error("Stripe dispute %s matches no payment on account %s", dispute.get("id"), account_id)
        return None
    status = dispute.get("status") or "unknown"
    with transaction.atomic():
        order = payments._lock_order_of(payment)  # the order first, then the payment: the order every path locks in
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        txn = payment.transactions.filter(
            transaction_type=DISPUTE, provider_transaction_id=dispute["id"], status=status
        )
        if not txn.exists():
            PaymentTransaction.objects.create(
                payment=payment,
                transaction_type=DISPUTE,
                amount=from_minor_units(dispute.get("amount") or 0, dispute.get("currency") or payment.currency),
                status=status,
                provider_transaction_id=dispute["id"],
                reason=dispute.get("reason"),
            )
        payment.metadata = {**(payment.metadata or {}), "dispute": {"id": dispute["id"], "status": status}}
        payment.save(update_fields=["metadata", "updated_at"])
        if order is not None:
            _flag_order_dispute(order, dispute, status, payment)
    logger.warning("Stripe dispute %s on payment %s is now %s", dispute["id"], payment.pk, status)
    return payment


def _flag_order_dispute(order, dispute, status, payment):
    """
    Mark the order so staff see the chargeback: Order.metadata["disputes"] (one entry per dispute, updated as it
    moves: needs_response, under_review, won, lost, ...). Nothing else changes: no stock, no order status, no
    refund. That is staff's decision. Repeats of the same event change nothing.
    """
    from django.utils import timezone

    entries = list((order.metadata or {}).get("disputes", []))
    current = next((e for e in entries if e.get("id") == dispute["id"]), None)
    if current is not None and current.get("status") == status:
        return
    now = timezone.now().isoformat()
    entry = {
        "id": dispute["id"],
        "status": status,
        "reason": dispute.get("reason"),
        "amount": str(Decimal(from_minor_units(dispute.get("amount") or 0, dispute.get("currency") or payment.currency)).quantize(Decimal("0.01"))),
        "currency": str(dispute.get("currency") or payment.currency).upper(),
        "payment": str(payment.pk),
        "opened_at": (current or {}).get("opened_at", now),
        "updated_at": now,
    }
    entries = [e for e in entries if e.get("id") != dispute["id"]] + [entry]
    order.metadata = {**(order.metadata or {}), "disputes": entries}
    order.save(update_fields=["metadata", "updated_at"])


def disconnect(account_id):
    """The store revoked access (or removed its keys): stop taking card payments for it."""
    store = Store.objects.filter(stripe_account_id=account_id).first()
    if store is None:
        return None
    clear_connection(store, remove_webhook=False)
    logger.warning("Store %s disconnected Stripe account %s", store.pk, account_id)
    return store


def note_unpaid(intent, account_id):
    """payment_intent.payment_failed / .canceled: no order exists yet, so there is nothing to undo but the stock hold."""
    intent = plain(intent)
    # A failed attempt can be retried on the same intent, so only a cancelled one gives the stock back now
    checkout_id = plain(intent.get("metadata")).get("checkout_id")
    # The metadata is written by whoever owns the Stripe account: only honour it for a checkout of that account's store
    if intent.get("status") == "canceled" and checkout_id and account_id:
        if Checkout.objects.filter(pk=checkout_id, store__stripe_account_id=account_id).exists():
            inventory.release_reservations(checkout_id)
        else:
            logger.error("Stripe payment %s on %s names checkout %s of another store", intent.get("id"), account_id, checkout_id)
    error = (intent.get("last_payment_error") or {}).get("message")
    logger.info("Stripe payment %s on %s ended unpaid (%s): %s", intent.get("id"), account_id, intent.get("status"), error)
