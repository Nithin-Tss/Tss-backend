"""Builds and sends the store's notification emails. Nothing here may break the order, payment or delivery behind it."""
import logging
from email.utils import formataddr

from django.conf import settings as django_settings

from apps.core.tasks import send_store_email
from apps.orders.receivers import confirmation_body, customer_email
from apps.tenancy.models import StoreMembership

from .models import NotificationSettings

logger = logging.getLogger(__name__)


def get_settings(store):
    return NotificationSettings.objects.get_or_create(store=store)[0]


def staff_emails(store, config):
    """Who staff notifications go to: the list set in Settings, else the store's contact email, else its owners."""
    if config.staff_recipients:
        return list(config.staff_recipients)
    if store.contact_email:
        return [store.contact_email]
    members = StoreMembership.objects.filter(store=store, role="owner", status="active").select_related("user")
    return sorted({m.user.email for m in members if m.user.email})


def _from_header(store, config):
    name = config.sender_name or store.store_name
    return formataddr((name, django_settings.DEFAULT_FROM_EMAIL))


def send(store, config, subject, lines, recipients):
    """Queue one email. `lines` is the body, one paragraph per item; the store's footer is added."""
    recipients = [r for r in recipients if r]
    if not recipients:
        return False
    body = "\n\n".join(lines)
    if config.email_footer:
        body += f"\n\n--\n{config.email_footer}"
    try:
        send_store_email(
            subject, body, recipients,
            from_email=_from_header(store, config), reply_to=config.reply_to_email or store.contact_email or "",
        )
    except Exception:
        logger.exception("Could not queue %r for store %s", subject, store.pk)
        return False
    return True


def _money(order):
    return f"{order.currency_code} {order.total}"


# ---------------------------------------------------------------- events

def order_placed(order):
    store, config = order.store, get_settings(order.store)
    email = customer_email(order)
    if config.customer_order_confirmation and email:
        send(store, config, f"Order {order.order_number} confirmed", [confirmation_body(order)], [email])
    if config.staff_new_order:
        send(store, config, f"New order {order.order_number}", [
            f"{store.store_name} received order {order.order_number}.",
            f"Total: {_money(order)}\nCustomer: {email or 'guest'}",
        ], staff_emails(store, config))


def order_cancelled(order):
    store, config = order.store, get_settings(order.store)
    email = customer_email(order)
    if config.customer_order_cancelled and email:
        send(store, config, f"Order {order.order_number} cancelled", [
            f"Your order {order.order_number} from {store.store_name} has been cancelled.",
            "If you paid, any refund will be sent to your original payment method.",
        ], [email])
    if config.staff_order_cancelled:
        send(store, config, f"Order {order.order_number} cancelled", [
            f"Order {order.order_number} was cancelled.",
        ], staff_emails(store, config))


def payment_refunded(payment, refunded):
    order = payment.order
    email = customer_email(order) if order else None
    if not email:
        return
    store, config = order.store, get_settings(order.store)
    if config.customer_order_refunded:
        send(store, config, f"Refund for order {order.order_number}", [
            f"We've refunded {payment.currency} {refunded} for your order {order.order_number}.",
            "It can take a few days to show on your statement.",
        ], [email])


def shipment_delivered(shipment):
    order = shipment.fulfillment.order
    email = customer_email(order)
    if not email:
        return
    store, config = order.store, get_settings(order.store)
    if config.customer_shipment_delivered:
        send(store, config, f"Order {order.order_number} was delivered", [
            f"Your order {order.order_number} from {store.store_name} has been delivered.",
        ], [email])


def send_test(store, to):
    config = get_settings(store)
    return send(store, config, f"Test email from {store.store_name}", [
        "This is a test of your store's notification settings.",
        "If you can read this, customers will see your sender name and reply address like this.",
    ], [to])
