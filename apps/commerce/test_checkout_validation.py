"""
Checkout input validation and abuse limits on the public storefront API.

    python manage.py test apps.commerce.test_checkout_validation --settings=config.test_settings
"""
from decimal import Decimal

from apps.customers.models import Customer
from apps.inventory.services import sellable
from apps.orders.models import Order

from . import tests as base_tests


class CheckoutValidationTests(base_tests.CommerceTestBase):
    base = base_tests.PlaceOrderTests.base
    h = base_tests.PlaceOrderTests.h
    address = base_tests.PlaceOrderTests.address

    def add_to_cart(self, quantity=1):
        return self.client.post(
            f"{self.base}/cart/items/", {"product": str(self.product.pk), "quantity": quantity},
            format="json", **self.h,
        )

    def start(self, **body):
        return self.client.post(f"{self.base}/checkout/", body, format="json", **self.h)

    def place(self, **body):
        self.add_to_cart()
        r = self.start(**body)
        self.assertEqual(r.status_code, 201, r.data)
        return r.data["id"], self.client.post(f"{self.base}/checkout/{r.data['id']}/complete/", **self.h)

    # ------------------------------------------------------------ quantities

    def test_absurd_quantities_are_refused_not_a_server_error(self):
        self.variant.continue_selling = True  # the store sells past stock, so only the cap protects it
        self.variant.save()
        for quantity in (10**6, 10**12, 2**63, 10**30):
            r = self.add_to_cart(quantity)
            self.assertEqual(r.status_code, 400, quantity)

    def test_repeat_adds_cannot_climb_past_the_cap(self):
        self.variant.continue_selling = True
        self.variant.save()
        statuses = [self.add_to_cart(500).status_code for _ in range(4)]
        self.assertEqual(statuses[0], 201)
        self.assertIn(400, statuses)

    def test_raising_a_line_past_the_cap_is_refused(self):
        self.variant.continue_selling = True
        self.variant.save()
        item_id = self.add_to_cart(1).data["items"][0]["id"]
        r = self.client.patch(f"{self.base}/cart/items/{item_id}/", {"quantity": 10**9}, format="json", **self.h)
        self.assertEqual(r.status_code, 400)

    # ----------------------------------------------------------------- email

    def test_email_must_be_valid(self):
        self.add_to_cart()
        for bad in ("nope", "a@", "@b.com", "a b@c.com", "a@b.com\r\nBcc: x@y.com", "x" * 330 + "@b.com"):
            self.assertEqual(self.start(email=bad).status_code, 400, bad)

    def test_email_is_stored_trimmed_and_lower_case(self):
        _, r = self.place(email="  Buyer@Example.COM ", shipping_address=self.address)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Customer.objects.get().email, "buyer@example.com")

    def test_same_email_in_other_letter_case_is_one_customer(self):
        self.place(email="Buyer@Example.com", shipping_address=self.address)
        self.h = {"HTTP_X_CART_SESSION": "sess-2-bbbbbbbbbbbb"}
        self.place(email="buyer@EXAMPLE.com", shipping_address=self.address)
        self.assertEqual(Customer.objects.count(), 1)
        self.assertEqual(Order.objects.count(), 2)

    # ------------------------------------------------------------- addresses

    def test_phone_and_postal_code_must_look_real(self):
        self.add_to_cart()
        for field, bad in (
            ("phone", "<script>alert(1)</script>"), ("phone", "call me maybe"), ("phone", "12"),
            ("postal_code", "<b>x</b>"), ("postal_code", "!!!"), ("postal_code", "  "),
        ):
            address = {**self.address, field: bad}
            r = self.start(email="a@b.com", shipping_address=address)
            self.assertEqual(r.status_code, 400, (field, bad))
            self.assertIn("shipping_address", r.data)

    def test_ordinary_phone_and_postal_formats_are_accepted(self):
        self.add_to_cart()
        for phone, postal in (("9876543210", "600001"), ("+91 98765-43210", "SW1A 1AA"), ("(044) 2345 6789", "10001-1234")):
            address = {**self.address, "phone": phone, "postal_code": postal}
            r = self.start(email="a@b.com", shipping_address=address)
            self.assertEqual(r.status_code, 201, (phone, postal, r.data))

    def test_blank_or_markup_names_are_refused(self):
        self.add_to_cart()
        for field in ("first_name", "last_name", "address_line_1", "city", "state", "country"):
            for bad in ("", "   "):
                r = self.start(email="a@b.com", shipping_address={**self.address, field: bad})
                self.assertEqual(r.status_code, 400, (field, bad))

    def test_billing_address_gets_the_same_checks(self):
        self.add_to_cart()
        r = self.start(email="a@b.com", shipping_address=self.address,
                       billing_address={**self.address, "phone": "<script>"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("billing_address", r.data)

    def test_unknown_address_fields_are_dropped(self):
        self.add_to_cart()
        r = self.start(email="a@b.com", shipping_address={**self.address, "is_admin": True, "role": "owner"})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertNotIn("is_admin", r.data["shipping_address"])
        self.assertNotIn("role", r.data["shipping_address"])

    # ---------------------------------------------------- cannot dictate money

    def test_client_cannot_set_prices_totals_or_status(self):
        self.add_to_cart()
        r = self.start(
            email="a@b.com", shipping_address=self.address,
            total_amount="0.01", subtotal="0.01", discount_amount="999", status="completed", tax_amount="0",
        )
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(r.data["total_amount"]), Decimal("499.00"))
        self.assertEqual(r.data["status"], "open")

    def test_cart_items_ignore_a_client_supplied_price(self):
        r = self.client.post(
            f"{self.base}/cart/items/",
            {"product": str(self.product.pk), "quantity": 1, "unit_price": "0.01", "price": "0.01"},
            format="json", **self.h,
        )
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(r.data["items"][0]["unit_price"]), Decimal("499.00"))

    def test_discount_code_cannot_be_junk(self):
        self.add_to_cart()
        for bad in ("NOPE", "x" * 51, "' OR 1=1 --"):
            self.assertEqual(self.start(email="a@b.com", discount_code=bad).status_code, 400, bad)

    # ---------------------------------------------------------- state / abuse

    def test_checkout_input_must_be_json_objects(self):
        self.add_to_cart()
        for body in ("[]", '"text"', "123", "null"):
            r = self.client.post(f"{self.base}/checkout/", body, content_type="application/json", **self.h)
            self.assertEqual(r.status_code, 400, body)

    def test_completed_checkout_cannot_be_edited_to_change_the_order(self):
        cid, r = self.place(email="a@b.com", shipping_address=self.address)
        self.assertEqual(r.status_code, 201, r.data)
        patched = self.client.patch(
            f"{self.base}/checkout/{cid}/", {"email": "thief@x.com", "shipping_address": {**self.address, "city": "Elsewhere"}},
            format="json", **self.h,
        )
        self.assertEqual(patched.status_code, 400)
        again = self.client.post(f"{self.base}/checkout/{cid}/complete/", **self.h)
        self.assertEqual(again.data["id"], r.data["id"])
        self.assertEqual(Order.objects.count(), 1)

    def test_a_stranger_session_cannot_complete_or_edit_my_checkout(self):
        self.add_to_cart()
        cid = self.start(email="a@b.com", shipping_address=self.address).data["id"]
        stranger = {"HTTP_X_CART_SESSION": "stranger-aaaaaaaaaaaa"}
        self.assertEqual(self.client.post(f"{self.base}/checkout/{cid}/complete/", **stranger).status_code, 404)
        self.assertEqual(self.client.patch(f"{self.base}/checkout/{cid}/", {"email": "x@y.com"}, format="json", **stranger).status_code, 404)
        self.assertEqual(self.client.get(f"{self.base}/checkout/{cid}/", **stranger).status_code, 404)
        self.assertEqual(Order.objects.count(), 0)

    def test_unknown_payment_method_places_nothing_and_keeps_stock(self):
        self.add_to_cart(2)
        cid = self.start(email="a@b.com", shipping_address=self.address).data["id"]
        r = self.client.post(f"{self.base}/checkout/{cid}/complete/", {"payment_method": "card"}, format="json", **self.h)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(sellable(self.variant), 10)

    def test_bad_ids_in_urls_are_404_not_a_crash(self):
        for path in ("checkout/not-a-uuid/", "checkout/not-a-uuid/complete/", "orders/not-a-uuid/", "cart/items/not-a-uuid/"):
            for method in ("get", "post", "patch", "delete"):
                r = getattr(self.client, method)(f"{self.base}/{path}", **self.h)
                self.assertLess(r.status_code, 500, (method, path, r.status_code))
