"""
Issuing a gift card that a customer bought as a normal product, through the normal cart and
checkout (see apps.commerce.services: Product.product_type == "gift_card" is special-cased
there to skip cash-on-delivery and to run this once the order is placed).

issue_from_order_item() only ever runs once: it is called from inside
apps.commerce.services._place_order, which itself only runs once per checkout -- a repeat
(a webhook retry, the shopper's return racing the webhook) is short-circuited before it gets
there and the same order is returned without this running again. So "no duplicates on retries"
falls out of the checkout's own idempotency, not anything specific to gift cards.
"""
from django.db import transaction

from . import emails, services
from .models import GiftCard, GiftCardTransaction


@transaction.atomic
def issue_from_order_item(order_item):
    """order_item is a line of the gift-card product: issue the card now that the order exists."""
    order = order_item.order
    store = order.store
    meta = order_item.metadata or {}

    buyer_email = order.customer_email or (order.customer.email if order.customer else "")
    recipient_email = (meta.get("recipient_email") or "").strip() or buyer_email
    recipient_name = (meta.get("recipient_name") or "").strip()
    message = (meta.get("message") or "").strip()

    code = services.generate_code(store)
    card = GiftCard.objects.create(
        store=store,
        code_hash=code["code_hash"],
        code_enc=code["code_enc"],
        code_last4=code["code_last4"],
        initial_balance=order_item.unit_price,
        current_balance=order_item.unit_price,
        currency_code=order.currency_code,
        status=services.ACTIVE,
        customer=order.customer,
        expires_at=None,  # a purchased card never expires
        is_deleted=False,
    )
    payment = order.payments.filter(is_deleted=False).order_by("created_at").first()
    GiftCardTransaction.objects.create(
        gift_card=card,
        transaction_type="credit",
        amount=order_item.unit_price,
        balance_before=0,
        balance_after=order_item.unit_price,
        order=order,
        payment=payment,
        reason="Purchased",
    )
    order_item.metadata = {**meta, "gift_card_id": str(card.pk)}
    order_item.save(update_fields=["metadata", "updated_at"])

    raw_code = services.reveal_code(card)
    emails.gift_card_issued(
        card, raw_code, recipient_email,
        recipient_name=recipient_name or None,
        message=message or None,
        from_name=None if recipient_email == buyer_email else buyer_email,
    )
    return card
