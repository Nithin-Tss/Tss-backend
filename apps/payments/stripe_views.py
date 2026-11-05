"""
Stripe endpoints.

  GET/POST /api/v1/payments/stripe/connect/   a store owner's connection to Stripe
  POST     /api/v1/payments/stripe/webhook/   Stripe's events (Connect webhook)
  POST     /api/v1/storefront/<store>/checkout/<id>/stripe/          start paying
  POST     /api/v1/storefront/<store>/checkout/<id>/stripe/confirm/  shopper is back from Stripe
"""
import json
import logging

import stripe
from django.conf import settings
from django.http import HttpResponse
from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.orders.serializers import ShopperOrderSerializer
from apps.commerce.storefront_views import CheckoutDetailView
from apps.core.permissions import HasStorePermission, IsStoreMember
from apps.core.rbac import PAYMENTS_MANAGE, PAYMENTS_VIEW

from apps.tenancy.models import Store
from apps.webhooks import incoming
from apps.webhooks.models import RECEIVED

from . import crypto, stripe_service
from .models import BankPayoutAccount
from .tasks import process_event  # noqa: F401  (kept importable; the webhook now goes through apps.webhooks.incoming)

logger = logging.getLogger(__name__)


class PaymentProviderUnavailable(APIException):
    status_code = 502
    default_detail = "We couldn't reach the card payment provider. Please try again in a moment."
    default_code = "payment_provider_unavailable"


class StripeErrorsMixin:
    """A Stripe API failure is a clean 502 with a safe message, never a crash that leaks details."""

    def handle_exception(self, exc):
        if isinstance(exc, stripe.StripeError):
            logger.error("Stripe call failed in %s: %s", type(self).__name__, exc, exc_info=exc)
            exc = PaymentProviderUnavailable()
        return super().handle_exception(exc)


def _status(store):
    key = crypto.decrypt(store.stripe_secret_key_enc)
    return {
        "connected": bool(store.stripe_account_id),
        "charges_enabled": store.stripe_charges_enabled,
        "details_submitted": store.stripe_details_submitted,
        "livemode": store.stripe_livemode,
        "publishable_key": store.stripe_publishable_key,
        "secret_key_last4": key[-4:] if key else "",
        "webhook_configured": bool(store.stripe_webhook_secret_enc),
        "webhook_url": stripe_service.webhook_url(store),
    }


class StripeConnectView(StripeErrorsMixin, APIView):
    """
    The store's own Stripe keys. POST validates what the owner typed (format, matching mode, and that Stripe accepts
    the secret key) before connecting; the secret key is stored encrypted and never returned.
    """

    permission_classes = [IsAuthenticated, IsStoreMember, HasStorePermission]
    permission_resource = "payments"
    action_permissions = {"GET": PAYMENTS_VIEW, "POST": PAYMENTS_MANAGE, "DELETE": PAYMENTS_MANAGE}

    def get(self, request):
        store = request.store
        if store.stripe_account_id:
            store = stripe_service.sync_account(store)
        return Response(_status(store))

    def post(self, request):
        d = request.data
        store = stripe_service.connect_with_keys(
            request.store, d.get("publishable_key"), d.get("secret_key"), d.get("webhook_secret")
        )
        return Response(_status(store))

    def delete(self, request):
        return Response(_status(stripe_service.clear_connection(request.store)))


class StoreStripeWebhookView(APIView):
    """POST /api/v1/payments/stripe/webhook/<store_id>/: events from a store's own Stripe account, signed with its secret."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = []

    @extend_schema(operation_id="payments_stripe_store_webhook_create", request=None, responses={200: None})
    def post(self, request, store_id):
        store = Store.objects.filter(pk=store_id).first()
        secret = crypto.decrypt(store.stripe_webhook_secret_enc) if store else ""
        if not secret:
            return HttpResponse(status=404)
        try:
            event = stripe.Webhook.construct_event(request.body, request.headers.get("Stripe-Signature", ""), secret)
        except (ValueError, stripe.SignatureVerificationError):
            return HttpResponse(status=400)
        data = event.to_dict() if hasattr(event, "to_dict") else dict(event)
        data["account"] = store.stripe_account_id  # direct events carry no account: it is this store's
        receipt, _ = incoming.record_receipt(incoming.STRIPE, data)
        if receipt.status == RECEIVED:
            incoming.enqueue(receipt)
        return HttpResponse(status=200)


class StripeWebhookView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = []

    @extend_schema(operation_id="payments_stripe_platform_webhook_create", request=None, responses={200: None})
    def post(self, request):
        if not settings.STRIPE_WEBHOOK_SECRET:
            return HttpResponse(status=503)
        try:
            event = stripe.Webhook.construct_event(
                request.body, request.headers.get("Stripe-Signature", ""), settings.STRIPE_WEBHOOK_SECRET
            )
        except (ValueError, stripe.SignatureVerificationError):
            return HttpResponse(status=400)

        # Signature is checked. Store the event in PostgreSQL first (a repeat of the same event id is stored once), then
        # answer; the work happens from the stored receipt in a background job, retried if it fails.
        receipt, _ = incoming.record_receipt(incoming.STRIPE, event.to_dict() if hasattr(event, "to_dict") else event)
        if receipt.status == RECEIVED:
            incoming.enqueue(receipt)
        return HttpResponse(status=200)


class CheckoutStripeView(StripeErrorsMixin, CheckoutDetailView):
    throttle_scope = "storefront_checkout"
    http_method_names = ["post", "options"]

    def post(self, request, store_slug, checkout_id):
        return Response(stripe_service.payment_session(self._checkout(request, checkout_id)))


class CheckoutStripeConfirmView(StripeErrorsMixin, CheckoutDetailView):
    """The shopper came back from paying. Asks Stripe itself, so it never trusts the browser."""

    throttle_scope = "storefront_checkout"
    http_method_names = ["post", "options"]

    def post(self, request, store_slug, checkout_id):
        checkout = self._checkout(request, checkout_id)
        intent_id = (checkout.metadata or {}).get("stripe_payment_intent")
        if not intent_id:
            raise ValidationError("No card payment was started for this checkout.")
        account_id = checkout.store.stripe_account_id
        intent = stripe_service.client(checkout.store).v1.payment_intents.retrieve(intent_id)
        if intent.status != stripe_service.SUCCEEDED:
            return Response({"status": intent.status}, status=409)
        order = stripe_service.finalize(intent, account_id)
        if order is None:
            raise ValidationError("Your payment was refunded because the order could not be placed.")
        return Response(ShopperOrderSerializer(order).data)


class BankPayoutView(APIView):
    """GET/PUT /api/v1/payments/bank-payout/: the store's bank details for payouts. The full account number is write-only."""

    permission_classes = [IsAuthenticated, IsStoreMember, HasStorePermission]
    permission_resource = "payments"
    action_permissions = {"GET": PAYMENTS_VIEW, "PUT": PAYMENTS_MANAGE, "DELETE": PAYMENTS_MANAGE}

    @staticmethod
    def _data(acct):
        if not acct:
            return {"configured": False}
        return {
            "configured": True,
            "account_holder_name": acct.account_holder_name,
            "bank_name": acct.bank_name,
            "account_last4": acct.account_last4,
            "routing_code": acct.routing_code,
            "currency": acct.currency,
        }

    def get(self, request):
        return Response(self._data(BankPayoutAccount.objects.filter(store=request.store).first()))

    def put(self, request):
        import re

        d = request.data
        existing = BankPayoutAccount.objects.filter(store=request.store).first()
        errors = {}

        def text(key, label, limit):
            v = str(d.get(key, "")).strip()
            if not v:
                errors[key] = f"{label} is required."
            elif len(v) > limit:
                errors[key] = f"{label} is too long."
            return v

        holder = text("account_holder_name", "Account holder name", 120)
        bank = text("bank_name", "Bank name", 120)
        routing = re.sub(r"[\s-]", "", str(d.get("routing_code", ""))).upper()
        if not re.fullmatch(r"[A-Z0-9]{4,20}", routing):
            errors["routing_code"] = "Enter a valid routing / sort / IFSC code."
        number = re.sub(r"[\s-]", "", str(d.get("account_number", ""))).upper()
        if number:
            if not re.fullmatch(r"[A-Z0-9]{4,34}", number):
                errors["account_number"] = "Enter a valid account number or IBAN."
        elif existing:
            number = existing.account_number  # left blank: keep the saved one
        else:
            errors["account_number"] = "Account number is required."
        currency = str(d.get("currency", "USD")).strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            errors["currency"] = "Enter a 3-letter currency code."
        if errors:
            raise ValidationError(errors)

        acct, _ = BankPayoutAccount.objects.update_or_create(
            store=request.store,
            defaults={
                "account_holder_name": holder, "bank_name": bank, "account_number": number,
                "routing_code": routing, "currency": currency,
            },
        )
        return Response(self._data(acct))

    def delete(self, request):
        BankPayoutAccount.objects.filter(store=request.store).delete()
        return Response(status=204)
