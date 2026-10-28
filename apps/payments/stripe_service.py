"""
Stripe, as a Connect platform. Each store connects its own Standard account and
shoppers pay it directly: charges are created on the connected account (the
`stripe_account` request option), so the money, the dispute liability and the
Stripe dashboard all belong to the store.

An order is only created once Stripe confirms the money (payment_intent.succeeded,
via the webhook or the shopper's return from Stripe, whichever comes first), so
unpaid checkouts never hold stock. If the order cannot be placed after the shopper
paid (stock ran out, the total moved), the payment is refunded straight away.
"""
import logging
from decimal import Decimal

import stripe
from django.conf import settings
from django.db import transaction
from rest_framework.exceptions import ValidationError

from apps.commerce import services as commerce
from apps.commerce.models import Checkout
from apps.tenancy.models import Store

from . import services as payments
from .models import Payment, PaymentTransaction

logger = logging.getLogger(__name__)

PROVIDER = "stripe"
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


def client():
    if not settings.STRIPE_SECRET_KEY:
        raise ValidationError("Card payments are not configured on this server.")
    return stripe.StripeClient(settings.STRIPE_SECRET_KEY)


def from_minor_units(amount, currency):
    amount = Decimal(amount)
    return amount if currency.upper() in ZERO_DECIMAL else amount / 100


def to_minor_units(amount, currency):
    amount = Decimal(amount)
    return int(amount) if currency.upper() in ZERO_DECIMAL else int(amount * 100)


def _require_connected(store):
    if not store.stripe_account_id or not store.stripe_charges_enabled:
        raise ValidationError("This store can't take card payments yet.")


# -- Connecting a store ------------------------------------------------------------------------

def onboarding_link(store, return_url, refresh_url):
    """Create the store's Standard account on first use; return a Stripe-hosted onboarding URL."""
    stripe_client = client()
    if not store.stripe_account_id:
        account = stripe_client.v1.accounts.create(
            {"type": "standard", "email": store.contact_email or None, "metadata": {"store_id": str(store.pk)}}
        )
        store.stripe_account_id = account.id
        store.save(update_fields=["stripe_account_id", "updated_at"])
    link = stripe_client.v1.account_links.create(
        {
            "account": store.stripe_account_id,
            "type": "account_onboarding",
            "return_url": return_url,
            "refresh_url": refresh_url,
        }
    )
    return link.url


def sync_account(store, account=None):
    """Copy whether the store may take payments from Stripe (account.updated, or after onboarding)."""
    if not store.stripe_account_id:
        return store
    account = account or client().v1.accounts.retrieve(store.stripe_account_id)
    store.stripe_charges_enabled = bool(account.charges_enabled)
    store.stripe_details_submitted = bool(account.details_submitted)
    store.save(update_fields=["stripe_charges_enabled", "stripe_details_submitted", "updated_at"])
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

    for item in checkout.items.filter(is_deleted=False).select_related("product", "variant"):
        commerce._check_stock(item.product, item.variant, item.quantity)

    store = checkout.store
    options = {"stripe_account": store.stripe_account_id}
    amount = to_minor_units(checkout.total_amount, checkout.currency)
    intents = client().v1.payment_intents
    intent_id = (checkout.metadata or {}).get("stripe_payment_intent")

    intent = None
    if intent_id:
        intent = intents.retrieve(intent_id, options=options)
        if intent.status == SUCCEEDED:
            raise ValidationError("This checkout has already been paid.")
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
        "publishable_key": settings.STRIPE_PUBLISHABLE_KEY,
        "stripe_account": store.stripe_account_id,
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
        try:
            refund_intent(intent.id, account_id, reason="order_not_placed")
        except Exception:
            # Raised on so Stripe retries the webhook (the refund key makes that safe), but say loudly that money is unplaced
            logger.exception("REFUND FAILED for Stripe payment %s on account %s: no order and money not returned", intent.id, account_id)
            raise
        return None


def refund_intent(intent_id, account_id, amount=None, reason=None):
    params = {"payment_intent": intent_id}
    if amount is not None:
        params["amount"] = amount
    if reason == "order_not_placed":
        params["metadata"] = {"reason": reason}
    key = f"refund-{intent_id}-{amount or 'full'}"
    return client().v1.refunds.create(params, options={"stripe_account": account_id, "idempotency_key": key})


def refund_payment(payment, amount, already_refunded):
    """Refund part or all of a stripe Payment on the store's account; returns the Stripe refund id."""
    account_id = payment.store.stripe_account_id
    if not account_id:
        raise ValidationError("This store is no longer connected to Stripe.")
    minor = to_minor_units(amount, payment.currency)
    # The same refund asked twice must not repeat: the key names this payment and how much is already refunded
    key = f"refund-{payment.pk}-{already_refunded}-{minor}"
    try:
        refund = client().v1.refunds.create(
            {"payment_intent": payment.provider_payment_id, "amount": minor},
            options={"stripe_account": account_id, "idempotency_key": key},
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
    logger.warning("Stripe dispute %s on payment %s is now %s", dispute["id"], payment.pk, status)
    return payment


def disconnect(account_id):
    """account.application.deauthorized: the store revoked us, so stop taking card payments for it."""
    store = Store.objects.filter(stripe_account_id=account_id).first()
    if store is None:
        return None
    store.stripe_account_id, store.stripe_charges_enabled, store.stripe_details_submitted = "", False, False
    store.save(update_fields=["stripe_account_id", "stripe_charges_enabled", "stripe_details_submitted", "updated_at"])
    logger.warning("Store %s disconnected Stripe account %s", store.pk, account_id)
    return store


def note_unpaid(intent, account_id):
    """payment_intent.payment_failed / .canceled: no order exists yet, so there is nothing to undo."""
    intent = plain(intent)
    error = (intent.get("last_payment_error") or {}).get("message")
    logger.info("Stripe payment %s on %s ended unpaid (%s): %s", intent.get("id"), account_id, intent.get("status"), error)
