"""The events a store can subscribe to, and how each one is described in the payload."""
from decimal import Decimal

ORDER_CREATED = "order.created"
ORDER_CANCELLED = "order.cancelled"
PAYMENT_CAPTURED = "payment.captured"
PAYMENT_REFUNDED = "payment.refunded"
CUSTOMER_CREATED = "customer.created"
PRODUCT_CREATED = "product.created"
FULFILLMENT_CREATED = "fulfillment.created"
SHIPMENT_CREATED = "shipment.created"
SHIPMENT_DELIVERED = "shipment.delivered"
CHECKOUT_ABANDONED = "checkout.abandoned"
PING = "ping"

EVENTS = {
    ORDER_CREATED: "An order was placed",
    ORDER_CANCELLED: "An order was cancelled",
    PAYMENT_CAPTURED: "A payment was received for an order",
    PAYMENT_REFUNDED: "A payment was refunded, in part or in full",
    CUSTOMER_CREATED: "A customer was added",
    PRODUCT_CREATED: "A product was added",
    FULFILLMENT_CREATED: "Items of an order were fulfilled",
    SHIPMENT_CREATED: "A fulfillment was handed to a delivery partner",
    SHIPMENT_DELIVERED: "A shipment was delivered",
    CHECKOUT_ABANDONED: "A shopper left a checkout without paying",
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
        "delivery_status": order.delivery_status,
        "delivery_method": order.delivery_method,
        "shipping_address": order.shipping_address,
        "billing_address": order.billing_address,
        "created_at": order.created_at.isoformat(),
        "items": [
            {
                "id": str(i.pk),
                "product_id": str(i.product_id),
                "variant_id": str(i.variant_id) if i.variant_id else None,
                "sku": i.sku,
                "product_name": i.product_name,
                "quantity": i.quantity,
                "unit_price": str(i.unit_price),
                "discount_amount": str(i.discount_amount),
                "tax_amount": str(i.tax_amount),
                "total": str(i.total),
            }
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


def describe_customer(customer):
    return {
        "id": str(customer.pk),
        "first_name": customer.first_name,
        "last_name": customer.last_name,
        "email": customer.email,
        "email_subscription_status": customer.email_subscription_status,
        "city": customer.city,
        "state": customer.state,
        "country": customer.country,
        "created_at": customer.created_at.isoformat(),
    }


def describe_product(product):
    return {
        "id": str(product.pk),
        "title": product.title,
        "status": product.status,
        "variants": [
            {"id": str(v.pk), "sku": v.sku, "price": str(v.price)}
            for v in product.variants.all()
        ],
    }


def describe_fulfillment(fulfillment):
    return {
        "id": str(fulfillment.pk),
        "order_id": str(fulfillment.order_id),
        "order_number": fulfillment.order.order_number,
        "status": fulfillment.status,
        "delivery_method": fulfillment.delivery_method,
        "items": [
            {"order_item_id": str(i.order_item_id), "product_name": i.order_item.product_name,
             "sku": i.order_item.sku, "quantity": i.quantity}
            for i in fulfillment.items.select_related("order_item")
        ],
    }


def describe_shipment(shipment):
    return {
        "id": str(shipment.pk),
        "fulfillment_id": str(shipment.fulfillment_id),
        "order_id": str(shipment.fulfillment.order_id),
        "order_number": shipment.fulfillment.order.order_number,
        "status": shipment.status,
        "tracking_number": shipment.tracking_number,
        "delivery_gateway": shipment.delivery_gateway.name,
        "shipped_at": shipment.shipped_at.isoformat() if shipment.shipped_at else None,
        "delivered_at": shipment.delivered_at.isoformat() if shipment.delivered_at else None,
    }


def describe_checkout(checkout):
    return {
        "id": str(checkout.pk),
        "email": checkout.email or None,
        "currency": checkout.currency,
        "total": str(checkout.total_amount),
        "items": [
            {"product_name": i.product.title, "quantity": i.quantity, "unit_price": str(i.unit_price)}
            for i in checkout.items.filter(is_deleted=False).select_related("product")
        ],
    }
