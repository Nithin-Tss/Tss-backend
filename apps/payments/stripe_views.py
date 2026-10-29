"""
Stripe endpoints.

  GET/POST /api/v1/payments/stripe/connect/   a store owner's connection to Stripe
  POST     /api/v1/payments/stripe/webhook/   Stripe's events (Connect webhook)
  POST     /api/v1/storefront/<store>/checkout/<id>/stripe/          start paying
  POST     /api/v1/storefront/<store>/checkout/<id>/stripe/confirm/  shopper is back from Stripe
"""
import logging

import stripe
from django.conf import settings
from django.http import HttpResponse
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.orders.serializers import OrderSerializer
from apps.commerce.storefront_views import CheckoutDetailView
from apps.core.permissions import HasStorePermission, IsStoreMember
from apps.core.rbac import PAYMENTS_MANAGE, PAYMENTS_VIEW
from apps.tenancy.models import Store

from . import stripe_service

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
    return {
        "connected": bool(store.stripe_account_id),
        "charges_enabled": store.stripe_charges_enabled,
        "details_submitted": store.stripe_details_submitted,
    }


class StripeConnectView(StripeErrorsMixin, APIView):
    """Connect or inspect Stripe (owners, or roles with the payments permissions); the page URLs come from our own FRONTEND_URL."""

    permission_classes = [IsAuthenticated, IsStoreMember, HasStorePermission]
    permission_resource = "payments"
    action_permissions = {"GET": PAYMENTS_VIEW, "POST": PAYMENTS_MANAGE}

    def get(self, request):
        store = request.store
        if store.stripe_account_id:
            store = stripe_service.sync_account(store)
        return Response(_status(store))

    def post(self, request):
        page = f"{settings.FRONTEND_URL.rstrip('/')}/admin/settings"
        url = stripe_service.onboarding_link(
            request.store, return_url=f"{page}?stripe=return", refresh_url=f"{page}?stripe=refresh"
        )
        return Response({"url": url})


class StripeWebhookView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = []

    def post(self, request):
        if not settings.STRIPE_WEBHOOK_SECRET:
            return HttpResponse(status=503)
        try:
            event = stripe.Webhook.construct_event(
                request.body, request.headers.get("Stripe-Signature", ""), settings.STRIPE_WEBHOOK_SECRET
            )
        except (ValueError, stripe.SignatureVerificationError):
            return HttpResponse(status=400)

        # construct_event returns a Stripe object, not a dict: no .get()
        account_id = event["account"] if "account" in event else None
        obj = event["data"]["object"]
        # An error here answers 5xx so Stripe retries; a payment we can't place is refunded inside finalize
        if event["type"] == "payment_intent.succeeded" and account_id:
            stripe_service.finalize(obj, account_id)
        elif event["type"] == "charge.refunded" and account_id:
            stripe_service.record_charge_refunds(obj, account_id)
        elif event["type"] in ("charge.dispute.created", "charge.dispute.closed") and account_id:
            stripe_service.record_dispute(obj, account_id)
        elif event["type"] in ("payment_intent.payment_failed", "payment_intent.canceled"):
            stripe_service.note_unpaid(obj, account_id)
        elif event["type"] == "account.application.deauthorized" and account_id:
            stripe_service.disconnect(account_id)
        elif event["type"] == "account.updated":
            store = Store.objects.filter(stripe_account_id=obj["id"]).first()
            if store:
                stripe_service.sync_account(store, account=obj)
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
        intent = stripe_service.client().v1.payment_intents.retrieve(intent_id, options={"stripe_account": account_id})
        if intent.status != stripe_service.SUCCEEDED:
            return Response({"status": intent.status}, status=409)
        order = stripe_service.finalize(intent, account_id)
        if order is None:
            raise ValidationError("Your payment was refunded because the order could not be placed.")
        return Response(OrderSerializer(order).data)
