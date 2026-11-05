"""The shopper's order confirmation email.

Sending it is now done by apps.notifications (Settings > Notifications), which can switch it off and sets the sender
name and reply address. This module keeps the wording of the message.
"""


def customer_email(order):
    return order.customer_email or getattr(order.customer, "email", None)


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
