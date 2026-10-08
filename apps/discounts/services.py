"""Discount rules: is a code usable right now, and how much does it take off?"""
from decimal import Decimal

from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import DiscountCode, OrderDiscount

PERCENTAGE, FIXED = "percentage", "fixed_amount"
TYPES = (PERCENTAGE, FIXED)


def find_code(store, code):
    """The active DiscountCode for `code`, or a ValidationError explaining why not."""
    match = (
        DiscountCode.objects.select_related("discount")
        .filter(
            discount__store=store,
            code__iexact=(code or "").strip(),
            is_deleted=False,
            discount__is_deleted=False,
        )
        .first()
    )
    if match is None or not match.is_active or not match.discount.is_active:
        raise ValidationError({"code": "This discount code isn't valid."})

    discount = match.discount
    now = timezone.now()

    if discount.starts_at and discount.starts_at > now:
        raise ValidationError({"code": "This discount hasn't started yet."})
    if discount.ends_at and discount.ends_at < now:
        raise ValidationError({"code": "This discount has expired."})
    if discount.usage_limit is not None and times_used(discount) >= discount.usage_limit:
        raise ValidationError({"code": "This discount has been fully used."})

    return match


def times_used(discount):
    return OrderDiscount.objects.filter(discount=discount, is_deleted=False).count()


def amount_for(discount, subtotal):
    """How much `discount` takes off an order with this subtotal."""
    subtotal = Decimal(str(subtotal))

    if discount.minimum_order_amount and subtotal < discount.minimum_order_amount:
        raise ValidationError(
            {"code": f"This discount needs an order of at least {discount.minimum_order_amount}."}
        )

    if discount.discount_type == PERCENTAGE:
        amount = subtotal * discount.value / Decimal("100")
    else:
        amount = discount.value

    if discount.maximum_discount_amount:
        amount = min(amount, discount.maximum_discount_amount)

    return min(amount, subtotal).quantize(Decimal("0.01"))
