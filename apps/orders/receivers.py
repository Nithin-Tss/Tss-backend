"""Reactions to order_placed: the shopper's confirmation email."""
import logging

from apps.core.tasks import send_email_async
from django.dispatch import receiver

from .services import order_placed

logger = logging.getLogger(__name__)


def confirmation_body(order):
    lines = [
        f"{item.quantity} x {item.product_name} - {item.total} {order.currency_code}"
        for item in order.items.filter(is_deleted=False)
    ]
    return "\n".join(
        [
            f"Thanks for your order {order.order_number} at {order.store.store_name}.",
            "",
            *lines,
            "",
            f"Total: {order.total} {order.currency_code}",
        ]
    )


@receiver(order_placed)
def email_confirmation(sender, order, **kwargs):
    # Runs after commit; a failed email must never undo or break the order
    email = order.customer_email or getattr(order.customer, "email", None)
    if not email:
        return
    try:
        send_email_async(f"Order {order.order_number} confirmed", confirmation_body(order), [email])
    except Exception:
        logger.exception("Could not send the confirmation for order %s", order.pk)
