from decimal import Decimal
from rest_framework import serializers

from apps.catalog.models import Product, ProductVariant
from apps.core.viewsets import StoreScopedSerializerMixin

from .models import DraftOrder, DraftOrderDetail, Order, OrderItem


class OrderItemSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()
    variant_title = serializers.SerializerMethodField()

    class Meta:
        model = OrderItem
        fields = [
            "id", "product", "variant", "product_name", "variant_title", "sku", "quantity",
            "unit_price", "discount_amount", "tax_amount", "total", "image_url",
        ]
        read_only_fields = fields

    def get_image_url(self, item):
        if item.product_id:
            img = item.product.images.order_by("position").first()
            if img:
                return img.url
        return None

    def get_variant_title(self, item):
        if item.variant_id and item.variant:
            return getattr(item.variant, "title", None) or getattr(item.variant, "sku", None)
        return None


class OrderSerializer(serializers.ModelSerializer):
    items = serializers.SerializerMethodField()
    customer_name = serializers.SerializerMethodField()
    customer_email = serializers.SerializerMethodField()
    customer_details = serializers.SerializerMethodField()
    channel = serializers.SerializerMethodField()
    items_count = serializers.SerializerMethodField()
    tags = serializers.SerializerMethodField()
    payment_status = serializers.SerializerMethodField()
    shipping_address = serializers.SerializerMethodField()
    billing_address = serializers.SerializerMethodField()
    fulfillments = serializers.SerializerMethodField()
    payments = serializers.SerializerMethodField()
    source_draft = serializers.SerializerMethodField()
    financial_summary = serializers.SerializerMethodField()
    timeline = serializers.SerializerMethodField()
    notes = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            "id", "order_number", "customer", "customer_name", "customer_email", "customer_details",
            "sales_channel", "channel", "currency_code",
            "subtotal", "discount_amount", "tax_amount", "shipping_amount", "total",
            "status", "payment_status", "fulfillment_status", "delivery_status", "delivery_method",
            "shipping_address", "billing_address", "fulfillments", "payments", "source_draft",
            "financial_summary", "timeline", "notes",
            "items", "items_count", "tags", "created_at", "updated_at",
        ]
        # Totals and statuses are calculated by the system, never typed in.
        read_only_fields = [f for f in fields if f != "delivery_method"]

    def get_items(self, order):
        return OrderItemSerializer(
            [i for i in order.items.all() if not i.is_deleted], many=True
        ).data

    def get_customer_name(self, order):
        c = order.customer
        if c is None:
            addr = self.get_shipping_address(order) or {}
            first = addr.get("first_name", "")
            last = addr.get("last_name", "")
            full = f"{first} {last}".strip()
            return full or order.customer_email or "Guest Customer"
        return " ".join(filter(None, [c.first_name, c.last_name])) or c.email

    def get_customer_email(self, order):
        return order.customer_email or (order.customer.email if order.customer else None) or ""

    def get_shipping_address(self, order):
        if order.shipping_address:
            return order.shipping_address
        try:
            from apps.commerce.models import Checkout
            checkout = Checkout.objects.filter(store=order.store, metadata__order_id=str(order.pk)).first()
            if checkout and checkout.shipping_address:
                return checkout.shipping_address
        except Exception:
            pass
        try:
            if order.customer:
                addr = order.customer.addresses.filter(address_type="shipping").first() or order.customer.addresses.first()
                if addr:
                    return {
                        "first_name": addr.first_name,
                        "last_name": addr.last_name,
                        "company": addr.company,
                        "address1": addr.address1,
                        "address2": addr.address2,
                        "city": addr.city,
                        "province": addr.province,
                        "state": addr.province,
                        "postal_code": addr.postal_code,
                        "country": addr.country,
                        "phone": addr.phone,
                    }
        except Exception:
            pass
        return None

    def get_billing_address(self, order):
        if order.billing_address:
            return order.billing_address
        try:
            from apps.commerce.models import Checkout
            checkout = Checkout.objects.filter(store=order.store, metadata__order_id=str(order.pk)).first()
            if checkout and checkout.billing_address:
                return checkout.billing_address
        except Exception:
            pass
        try:
            if order.customer:
                addr = order.customer.addresses.filter(address_type="billing").first()
                if addr:
                    return {
                        "first_name": addr.first_name,
                        "last_name": addr.last_name,
                        "company": addr.company,
                        "address1": addr.address1,
                        "address2": addr.address2,
                        "city": addr.city,
                        "province": addr.province,
                        "state": addr.province,
                        "postal_code": addr.postal_code,
                        "country": addr.country,
                        "phone": addr.phone,
                    }
        except Exception:
            pass
        return None

    def get_customer_details(self, order):
        c = order.customer
        email = self.get_customer_email(order)
        name = self.get_customer_name(order)
        shipping_addr = self.get_shipping_address(order) or {}
        phone = (shipping_addr.get("phone") or (getattr(c, "phone_number", "") if c else "") or "")
        orders_count = c.orders.count() if c else (1 if order.pk else 0)
        return {
            "id": str(c.id) if c else None,
            "name": name,
            "email": email,
            "phone": phone,
            "orders_count": orders_count,
            "is_guest": c is None,
        }

    def get_fulfillments(self, order):
        try:
            from apps.fulfillment.serializers import FulfillmentSerializer
            return FulfillmentSerializer(order.fulfillments.all(), many=True).data
        except Exception:
            return []

    def get_payments(self, order):
        try:
            from apps.payments.serializers import PaymentSerializer
            return PaymentSerializer(order.payments.all(), many=True).data
        except Exception:
            return []

    def get_source_draft(self, order):
        try:
            draft = order.source_draft.first() if hasattr(order, "source_draft") else None
            if draft:
                return {
                    "id": str(draft.id),
                    "draft_number": draft.draft_number,
                    "created_by": draft.created_by_user.email if draft.created_by_user else None,
                }
        except Exception:
            pass
        return None

    def get_notes(self, order):
        try:
            from apps.commerce.models import Checkout
            checkout = Checkout.objects.filter(store=order.store, metadata__order_id=str(order.pk)).first()
            if checkout and checkout.metadata and checkout.metadata.get("notes"):
                return checkout.metadata.get("notes")
        except Exception:
            pass
        return getattr(order, "notes", "") or ""

    def get_financial_summary(self, order):
        payments = [p for p in order.payments.all() if not p.is_deleted] if hasattr(order, "payments") else []
        eligible = ("captured", "partially_refunded", "refunded")
        paid_payments = [p for p in payments if p.status in eligible]
        gross_paid = sum(p.amount for p in paid_payments) if paid_payments else Decimal("0.00")
        amount_refunded = sum(
            t.amount
            for p in payments
            for t in p.transactions.all()
            if t.transaction_type == "refund" and t.status == "success"
        ) if payments else Decimal("0.00")
        net_payment = max(gross_paid - amount_refunded, Decimal("0.00"))
        balance_due = max(order.total - net_payment, Decimal("0.00"))

        return {
            "subtotal": str(order.subtotal),
            "discount_amount": str(order.discount_amount),
            "tax_amount": str(order.tax_amount),
            "shipping_amount": str(order.shipping_amount),
            "total": str(order.total),
            "gross_paid": str(gross_paid),
            "amount_refunded": str(amount_refunded),
            "net_payment": str(net_payment),
            "balance_due": str(balance_due),
        }

    def get_timeline(self, order):
        events = []
        created_at_str = order.created_at.isoformat() if order.created_at else ""
        channel = order.sales_channel.name if order.sales_channel else "Online Store"
        draft = order.source_draft.first() if hasattr(order, "source_draft") else None
        cust_name = self.get_customer_name(order)
        cust_email = self.get_customer_email(order)

        if draft:
            creator = draft.created_by_user.email if draft.created_by_user else "Staff"
            events.append({
                "id": f"draft_order_{order.id}",
                "type": "draft_created",
                "title": f"{creator} created this order for {cust_name or 'customer'} from draft order #{draft.draft_number}.",
                "timestamp": created_at_str,
            })
        else:
            placed_by = cust_name if cust_name else "A customer"
            events.append({
                "id": f"placed_{order.id}",
                "type": "order_placed",
                "title": f"{placed_by} placed this order on {channel}.",
                "timestamp": created_at_str,
            })

        events.append({
            "id": f"conf_{order.id}",
            "type": "order_confirmation",
            "title": f"Confirmation #{order.order_number} was generated for this order.",
            "timestamp": created_at_str,
        })

        if cust_email:
            events.append({
                "id": f"email_{order.id}",
                "type": "order_email",
                "title": f"Order confirmation email was sent to {cust_name or 'the customer'} ({cust_email}).",
                "email": cust_email,
                "order_number": order.order_number,
                "timestamp": created_at_str,
            })

        if hasattr(order, "payments"):
            for p in order.payments.all():
                p_time = p.created_at.isoformat() if p.created_at else created_at_str
                provider_name = (p.provider or "Shop Pay").title()
                if p.status in ("captured", "paid"):
                    events.append({
                        "id": f"pay_{p.id}",
                        "type": "payment_processed",
                        "title": f"A {order.currency_code} {p.amount} payment was processed using {provider_name}.",
                        "timestamp": p_time,
                    })
                elif p.status == "pending":
                    events.append({
                        "id": f"pay_pend_{p.id}",
                        "type": "payment_pending",
                        "title": f"Payment of {order.currency_code} {p.amount} is pending ({provider_name}).",
                        "timestamp": p_time,
                    })
                for t in p.transactions.all():
                    t_time = t.created_at.isoformat() if t.created_at else p_time
                    if t.transaction_type == "refund" and t.status == "success":
                        events.append({
                            "id": f"ref_{t.id}",
                            "type": "refund_processed",
                            "title": f"A {order.currency_code} {t.amount} refund was processed. {f'Reason: {t.reason}' if t.reason else ''}".strip(),
                            "timestamp": t_time,
                        })

        if hasattr(order, "fulfillments"):
            for f in order.fulfillments.all():
                f_time = f.created_at.isoformat() if f.created_at else created_at_str
                item_count = sum(i.quantity for i in f.items.all())
                events.append({
                    "id": f"ful_{f.id}",
                    "type": "fulfillment_created",
                    "title": f"Marked {item_count} item{'s' if item_count != 1 else ''} as fulfilled.",
                    "timestamp": f_time,
                })

        if order.status == "cancelled":
            events.append({
                "id": f"canc_{order.id}",
                "type": "order_cancelled",
                "title": "This order was canceled.",
                "timestamp": order.updated_at.isoformat() if order.updated_at else created_at_str,
            })
        elif order.status in ("closed", "archived"):
            events.append({
                "id": f"arch_{order.id}",
                "type": "order_archived",
                "title": "This order was archived.",
                "timestamp": order.updated_at.isoformat() if order.updated_at else created_at_str,
            })

        events.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        return events

    def get_channel(self, order):
        return order.sales_channel.name if order.sales_channel else "Online Store"

    def get_items_count(self, order):
        return sum(i.quantity for i in order.items.all() if not i.is_deleted)

    def get_tags(self, order):
        return getattr(order, "tags", []) if hasattr(order, "tags") else []

    def get_payment_status(self, order):
        payments = [
            payment
            for payment in order.payments.all()
            if not payment.is_deleted
        ]

        if not payments:
            return "paid" if order.total <= 0 else "pending"

        eligible_statuses = (
            "captured",
            "partially_refunded",
            "refunded",
        )

        paid_payments = [
            payment
            for payment in payments
            if payment.status in eligible_statuses
        ]

        if not paid_payments:
            return "pending"

        amount_paid = sum(
            payment.amount - sum(
                transaction.amount
                for transaction in payment.transactions.all()
                if transaction.transaction_type == "refund"
                and transaction.status == "success"
            )
            for payment in paid_payments
        )

        # A full payment balance takes priority over historical refunds.
        if amount_paid >= order.total:
            return "paid"

        if any(
            payment.status in ("partially_refunded", "refunded")
            for payment in paid_payments
        ):
            return (
                "refunded"
                if amount_paid <= 0
                else "partially_refunded"
            )

        return "partially_paid" if amount_paid > 0 else "pending"


class OrderLineInputSerializer(StoreScopedSerializerMixin, serializers.Serializer):
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())
    variant = serializers.PrimaryKeyRelatedField(
        queryset=ProductVariant.objects.all(), required=False, allow_null=True
    )
    quantity = serializers.IntegerField(min_value=1)
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, required=False)
    discount_amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, required=False)


class CurrencyCodeField(serializers.CharField):
    def to_internal_value(self, data):
        value = super().to_internal_value(data).strip().upper()
        if len(value) != 3 or not value.isalpha():
            raise serializers.ValidationError("Enter a valid three-letter currency code.")
        return value


class OrderCreateSerializer(StoreScopedSerializerMixin, serializers.ModelSerializer):
    """A manual order created by staff in the admin."""

    items = OrderLineInputSerializer(many=True)
    discount_amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, default=0)
    tax_amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, default=0)
    shipping_amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, default=0)
    currency_code = CurrencyCodeField(max_length=3, required=False)

    class Meta:
        model = Order
        fields = [
            "customer", "sales_channel", "currency_code", "discount_amount",
            "tax_amount", "shipping_amount", "delivery_method", "items",
        ]


class DraftOrderDetailSerializer(StoreScopedSerializerMixin, serializers.ModelSerializer):
    product = serializers.PrimaryKeyRelatedField(
        queryset=Product.objects.all(), required=False, allow_null=True
    )
    variant = serializers.PrimaryKeyRelatedField(
        queryset=ProductVariant.objects.all(), required=False, allow_null=True
    )
    unit_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=0, required=False
    )
    discount_amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=0, required=False, allow_null=True
    )

    class Meta:
        model = DraftOrderDetail
        fields = ["id", "product", "variant", "title", "quantity", "unit_price", "discount_amount", "total"]
        read_only_fields = ["total"]
        extra_kwargs = {
            "quantity": {"min_value": 1},
            "title": {"required": False},
        }

    def validate(self, attrs):
        product = attrs.get("product")
        variant = attrs.get("variant")
        if variant is not None and product is None:
            raise serializers.ValidationError(
                {"product": "A product is required when a variant is selected."}
            )
        if variant is not None and variant.product_id != product.pk:
            raise serializers.ValidationError(
                {"variant": "The variant doesn't belong to the selected product."}
            )
        if product is None and not attrs.get("title", "").strip():
            raise serializers.ValidationError(
                {"title": "Custom items need a title."}
            )

        unit_price = attrs.get("unit_price")
        discount = attrs.get("discount_amount") or 0
        if unit_price is not None and discount > unit_price * attrs.get("quantity", 1):
            raise serializers.ValidationError(
                {"discount_amount": "The discount can't exceed the item subtotal."}
            )
        return attrs


class DraftOrderSerializer(StoreScopedSerializerMixin, serializers.ModelSerializer):
    details = DraftOrderDetailSerializer(many=True, required=False)
    created_by = serializers.CharField(source="created_by_user.email", read_only=True)
    customer_name = serializers.SerializerMethodField()
    currency_code = CurrencyCodeField(max_length=3, default="INR")

    class Meta:
        model = DraftOrder
        fields = [
            "id", "draft_number", "customer", "customer_name", "po_number", "status", "total", "currency_code",
            "order", "created_by", "details", "created_at", "updated_at",
        ]
        read_only_fields = ["draft_number", "status", "total", "order", "created_by"]

    def get_customer_name(self, draft):
        c = draft.customer
        if c is None:
            return ""
        return " ".join(filter(None, [c.first_name, c.last_name])) or c.email
