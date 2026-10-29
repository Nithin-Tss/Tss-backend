"""The events a store can subscribe to, and how each one is described in the payload."""
from decimal import Decimal

ORDER_CREATED = "order.created"
ORDER_CANCELLED = "order.cancelled"
PAYMENT_CAPTURED = "payment.captured"
PAYMENT_REFUNDED = "payment.refunded"
PING = "ping"

EVENTS = {
    ORDER_CREATED: "An order was placed",
    ORDER_CANCELLED: "An order was cancelled",
    PAYMENT_CAPTURED: "A payment was received for an order",
    PAYMENT_REFUNDED: "A payment was refunded, in part or in full",
}


def describe_order(order):
    return {
        "id": str(order.pk),
        "order_number": order.order_number,
        "status": order.status,
        "fulfillment_status": order.fulfillment_status,
        "currency": order.currency_code,
        "subtotal": str(order.subtotal),
        "discount_amount": str(order.discount_amount),
        "tax_amount": str(order.tax_amount),
        "shipping_amount": str(order.shipping_amount),
        "total": str(order.total),
        "customer_id": str(order.customer_id) if order.customer_id else None,
        "customer_email": order.customer_email or None,
        "items": [
            {"product_name": i.product_name, "quantity": i.quantity, "total": str(i.total)}
            for i in order.items.filter(is_deleted=False)
        ],
    }


def describe_payment(payment, refunded=None):
    data = {
        "id": str(payment.pk),
        "order_id": str(payment.order_id) if payment.order_id else None,
        "provider": payment.provider,
        "status": payment.status,
        "currency": payment.currency,
        "amount": str(payment.amount),
    }
    if refunded is not None:
        data["refunded"] = str(Decimal(refunded).quantize(Decimal("0.01")))
    return data
