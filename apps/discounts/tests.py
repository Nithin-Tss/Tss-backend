from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.core.tests.test_flow import StoreClientMixin
from apps.tenancy.models import Store

from .models import Discount

URL = "/api/v1/discounts/"


class DiscountValidationApiTests(StoreClientMixin, APITestCase):
    def setUp(self):
        _, store = self.sign_up("discounts@example.com", "Discount Store")
        self.store = Store.objects.get(pk=store["storeId"])

    def payload(self, **extra):
        return {"name": "Ten off", "discount_type": "percentage", "value": "10", **extra}

    def refused(self, field, **extra):
        r = self.client.post(URL, self.payload(**extra), format="json")
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn(field, r.data)
        self.assertEqual(Discount.objects.count(), 0)

    def test_negative_value_is_refused(self):
        self.refused("value", value="-1")
        self.refused("value", discount_type="fixed_amount", value="-0.01")

    def test_negative_optional_amounts_are_refused(self):
        self.refused("minimum_order_amount", minimum_order_amount="-1")
        self.refused("maximum_discount_amount", maximum_discount_amount="-5")

    def test_usage_limits_below_one_are_refused(self):
        for bad in (0, -1):
            self.refused("usage_limit", usage_limit=bad)
            self.refused("per_customer_limit", per_customer_limit=bad)

    def test_percentage_above_100_is_still_refused(self):
        self.refused("value", value="101")

    def test_zero_and_positive_values_are_accepted(self):
        d = self.ok("post", URL, self.payload(
            value="0", minimum_order_amount="0", maximum_discount_amount="0",
        ), expect=201)
        self.assertEqual((d["value"], d["minimum_order_amount"], d["maximum_discount_amount"]), ("0.00", "0.00", "0.00"))

        d = self.ok("post", URL, self.payload(
            discount_type="fixed_amount", value="150", minimum_order_amount="500",
            maximum_discount_amount="100", usage_limit=1, per_customer_limit=1,
        ), expect=201)
        self.assertEqual((d["usage_limit"], d["per_customer_limit"]), (1, 1))

    def test_optional_fields_accept_null(self):
        d = self.ok("post", URL, self.payload(
            minimum_order_amount=None, maximum_discount_amount=None, usage_limit=None, per_customer_limit=None,
        ), expect=201)
        for field in ("minimum_order_amount", "maximum_discount_amount", "usage_limit", "per_customer_limit"):
            self.assertIsNone(d[field], field)

    def test_update_with_negative_values_is_refused(self):
        d = self.ok("post", URL, self.payload(), expect=201)
        for field, bad in (("value", "-1"), ("minimum_order_amount", "-1"),
                           ("maximum_discount_amount", "-1"), ("usage_limit", 0), ("per_customer_limit", 0)):
            r = self.client.patch(f"{URL}{d['id']}/", {field: bad}, format="json")
            self.assertEqual(r.status_code, 400, field)
            self.assertIn(field, r.data)
        self.assertEqual(Discount.objects.get(pk=d["id"]).value, Decimal("10.00"))


class DiscountConstraintTests(StoreClientMixin, APITestCase):
    """The database refuses bad values even when the API is bypassed."""

    def setUp(self):
        _, store = self.sign_up("constraints@example.com", "Constraint Store")
        self.store = Store.objects.get(pk=store["storeId"])

    def make(self, **extra):
        fields = dict(
            store=self.store, name="X", discount_type="fixed_amount", value=Decimal("5"),
            starts_at=timezone.now() - timedelta(days=1), is_active=True, is_deleted=False,
        )
        fields.update(extra)
        return Discount.objects.create(**fields)

    def test_invalid_values_violate_check_constraints(self):
        for bad in (
            {"value": Decimal("-1")},
            {"minimum_order_amount": Decimal("-1")},
            {"maximum_discount_amount": Decimal("-1")},
            {"usage_limit": 0},
            {"per_customer_limit": 0},
        ):
            with self.subTest(bad=bad), self.assertRaises(IntegrityError), transaction.atomic():
                self.make(**bad)
        self.assertEqual(Discount.objects.count(), 0)

    def test_null_zero_and_minimum_values_satisfy_constraints(self):
        self.make()
        self.make(value=Decimal("0"), minimum_order_amount=Decimal("0"), maximum_discount_amount=Decimal("0"),
                  usage_limit=1, per_customer_limit=1)
        self.assertEqual(Discount.objects.count(), 2)
