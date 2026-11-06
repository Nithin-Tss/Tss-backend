import json
import logging

import stripe
from celery import shared_task
from django.conf import settings
from rest_framework.exceptions import ValidationError

from apps.tenancy.models import Store

from . import stripe_service

logger = logging.getLogger(__name__)

# Retrying can't fix a payload or a rule that is wrong; only outages and lock timeouts are worth another go
PERMANENT = (ValueError, KeyError, TypeError, ValidationError)


@shared_task(bind=True, max_retries=8)
def process_event(self, payload):
    """Handle a Stripe event whose signature the webhook view already verified. Handlers are safe to repeat."""
    try:
        _handle(json.loads(payload))
    except Exception as exc:
        if self.request.is_eager:  # no broker: fail the request so Stripe retries, as before
            raise
        if isinstance(exc, PERMANENT) or self.request.retries >= self.max_retries:
            # Nobody is retrying this any more: it needs a person (money may be taken with no order)
            logger.critical("Stripe event dropped, needs attention: %s", payload[:500], exc_info=exc)
            raise
        raise self.retry(exc=exc, countdown=min(2 ** self.request.retries * 15, 900))


def _handle(data):
    event = stripe.Event.construct_from(data, settings.STRIPE_SECRET_KEY)
    # construct_from returns a Stripe object, not a dict: no .get()
    account_id = event["account"] if "account" in event else None
    obj = event["data"]["object"]
    if event["type"] == "payment_intent.succeeded" and account_id:
        if stripe_service.plain(obj.metadata).get("gift_card_purchase_id"):
            from apps.gift_cards import purchases as gift_card_purchases

            gift_card_purchases.finalize_purchase(obj, account_id)
        else:
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
