"""
End-to-end commerce flows: the public storefront API and the staff API working
against the same store, with real sign-up, store membership and permissions.
"""
import threading
import uuid
import unittest
from datetime import timedelta
from io import StringIO
from unittest import mock
from decimal import Decimal

from django.core.cache import cache
from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient, APITestCase

from apps.catalog.models import Product, ProductSalesChannel, ProductVariant
from apps.catalog.services import online_channels
from apps.discounts.models import Discount, DiscountCode
from apps.inventory.models import InventoryHistory, InventoryItem, Location
from apps.inventory.services import default_location, sellable, set_available
from apps.orders import services as orders_services
from apps.orders.models import Order
from apps.payments.models import Payment
from apps.tenancy.models import Store

from . import services
from .tests import assert_stock_consistent
from .models import Cart, Checkout

ADDRESS = {"first_name": "A", "last_name": "B", "address_line_1": "1 Main St", "city": "Chennai", "state": "TN", "postal_code": "600001", "country": "India"}


def sign_up_with_store(email):
    """A signed-in staff client for a freshly created store; returns (client, Store)."""
    cache.clear()  # sign-ups are rate limited
    client = APIClient()
    r = client.post("/api/v1/auth/signup/", {
        "firstName": "A", "lastName": "B", "email": email,
        "password": "Sturdy-pass-123", "confirmPassword": "Sturdy-pass-123",
        "mobileNumber": "9876543210", "agreeTerms": True,
    }, format="json")
    client.credentials(HTTP_AUTHORIZATION="Bearer " + r.json()["access"])
    r = client.post("/api/v1/stores/", {"storeName": "Shop " + email.split("@")[0]}, format="json")
    store = Store.objects.get(pk=r.json()["storeId"])
    client.credentials(HTTP_AUTHORIZATION=client._credentials["HTTP_AUTHORIZATION"], HTTP_X_STORE_ID=str(store.pk))
    return client, store


def stock_product(store, title, sku, price, quantity, location):
    product = Product.objects.create(store=store, title=title, status="active")
    variant = ProductVariant.objects.create(product=product, sku=sku, price=Decimal(price))
    for channel in online_channels(store):
        ProductSalesChannel.objects.create(product=product, sales_channel=channel)
    set_available(variant, location, quantity)
    return product, variant


class CommerceIntegrationBase(APITestCase):
    def setUp(self):
        cache.clear()
        self.staff, self.store = sign_up_with_store("owner@example.com")
        self.store.status = "active"
        self.store.save()
        self.base = f"/api/v1/storefront/{self.store.store_slug}"
        self.location = Location.objects.create(store=self.store, name="Main warehouse")
        self.product, self.variant = stock_product(self.store, "Tee", "TEE-1", "499.00", 10, self.location)

        self.shopper = APIClient()
        self.shopper.credentials(HTTP_X_CART_SESSION="sess-1-aaaaaaaaaaaa")

    def add(self, product=None, quantity=1, client=None, **extra):
        return (client or self.shopper).post(
            f"{self.base}/cart/items/",
            {"product": str((product or self.product).pk), "quantity": quantity, **extra},
            format="json",
        )

    def checkout(self, quantity=2, client=None, **extra):
        self.add(quantity=quantity, client=client)
        r = (client or self.shopper).post(f"{self.base}/checkout/", extra, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return r.data["id"]

    def place(self, cid, client=None):
        return (client or self.shopper).post(f"{self.base}/checkout/{cid}/complete/")


class ShopperToStaffJourneyTests(CommerceIntegrationBase):
    def test_order_placed_by_shopper_is_visible_to_staff(self):
        cid = self.checkout(email="a@b.com", shipping_address=ADDRESS)

        # Staff see the checkout in progress, read-only
        r = self.staff.get("/api/v1/checkouts/")
        self.assertEqual([c["id"] for c in r.json()], [cid])
        self.assertEqual(self.staff.post("/api/v1/checkouts/", {}, format="json").status_code, 405)
        self.assertEqual(self.staff.get("/api/v1/carts/?status=open").json()[0]["session_id"], "sess-1-aaaaaaaaaaaa")

        order = self.place(cid)
        self.assertEqual(order.status_code, 201, order.data)

        # The cart and checkout close, the order shows up in the back office
        self.assertEqual(self.staff.get("/api/v1/carts/?status=open").json(), [])
        self.assertEqual(self.staff.get("/api/v1/carts/?status=checked_out").json()[0]["session_id"], "sess-1-aaaaaaaaaaaa")
        self.assertEqual(self.staff.get("/api/v1/checkouts/?status=completed").json()[0]["id"], cid)
        self.assertEqual(Order.objects.get().total, Decimal("998.00"))
        self.assertEqual(sellable(self.variant), 8)

        # A returning session starts from an empty cart
        self.assertEqual(self.shopper.get(f"{self.base}/cart/").data["items"], [])

    def test_order_links_its_checkout_and_keeps_email_and_addresses(self):
        billing = {**ADDRESS, "city": "Madurai"}
        cid = self.checkout(email="a@b.com", shipping_address=ADDRESS, billing_address=billing)
        self.assertEqual(self.place(cid).status_code, 201)

        order = Order.objects.get()
        self.assertEqual(str(order.checkout_id), cid)
        self.assertEqual(order.customer_email, "a@b.com")
        self.assertEqual(order.shipping_address, ADDRESS)
        self.assertEqual(order.billing_address, billing)

        # Staff orders have no checkout, so nothing to link or copy
        r = self.staff.post("/api/v1/orders/", {"items": [{"product": str(self.product.pk), "quantity": 1}]}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        staff_order = Order.objects.get(pk=r.data["id"])
        self.assertEqual(
            (staff_order.checkout, staff_order.customer_email, staff_order.shipping_address, staff_order.billing_address),
            (None, None, None, None),
        )

    def test_staff_api_requires_sign_in_and_membership(self):
        anonymous = APIClient()
        self.assertIn(anonymous.get("/api/v1/carts/").status_code, (401, 403))

        outsider, _ = sign_up_with_store("outsider@example.com")
        outsider.credentials(
            HTTP_AUTHORIZATION=outsider._credentials["HTTP_AUTHORIZATION"], HTTP_X_STORE_ID=str(self.store.pk)
        )
        self.assertEqual(outsider.get("/api/v1/carts/").status_code, 403)

    def test_each_store_sees_only_its_own_carts(self):
        self.add(quantity=1)
        other_staff, other_store = sign_up_with_store("other@example.com")
        self.assertEqual(other_staff.get("/api/v1/carts/").json(), [])
        self.assertEqual(other_staff.get("/api/v1/checkouts/").json(), [])
        cart = Cart.objects.get()
        self.assertEqual(other_staff.get(f"/api/v1/carts/{cart.pk}/").status_code, 404)
        self.assertEqual(len(self.staff.get("/api/v1/carts/").json()), 1)


class StoreIsolationTests(CommerceIntegrationBase):
    def setUp(self):
        super().setUp()
        _, self.other_store = sign_up_with_store("other@example.com")
        self.other_store.status = "active"
        self.other_store.save()
        other_location = Location.objects.create(store=self.other_store, name="Other warehouse")
        self.foreign, _ = stock_product(self.other_store, "Hat", "HAT-1", "99.00", 5, other_location)

    def test_cannot_add_another_stores_product(self):
        r = self.add(product=self.foreign)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Cart.objects.count(), 0)

    def test_same_session_gets_a_separate_cart_per_store(self):
        self.add()
        r = self.shopper.get(f"/api/v1/storefront/{self.other_store.store_slug}/cart/")
        self.assertEqual(r.data["items"], [])
        self.assertEqual(Cart.objects.count(), 2)

    def test_checkout_of_one_store_is_hidden_from_another(self):
        cid = self.checkout(email="a@b.com", shipping_address=ADDRESS)
        r = self.shopper.get(f"/api/v1/storefront/{self.other_store.store_slug}/checkout/{cid}/")
        self.assertEqual(r.status_code, 404)

    def test_inactive_store_is_not_shoppable(self):
        self.store.status = "suspended"
        self.store.save()
        self.assertEqual(self.shopper.get(f"{self.base}/cart/").status_code, 404)


class VariantAndStockTests(CommerceIntegrationBase):
    def setUp(self):
        super().setUp()
        self.large = ProductVariant.objects.create(product=self.product, sku="TEE-L", price=Decimal("549.00"))
        set_available(self.large, self.location, 3)

    def test_chosen_variant_sets_price_and_stock_limit(self):
        r = self.add(quantity=2, variant=str(self.large.pk))
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["items"][0]["unit_price"], "549.00")

        over = self.add(quantity=2, variant=str(self.large.pk))  # 2 + 2 > 3
        self.assertEqual(over.status_code, 400)
        self.assertIn("Only 3 of Tee left", str(over.data))

        # the other variant is unaffected
        self.assertEqual(self.add(quantity=5).status_code, 201)

    def test_variant_of_another_product_is_rejected(self):
        cap, cap_variant = stock_product(self.store, "Cap", "CAP-1", "199.00", 5, self.location)
        r = self.add(quantity=1, variant=str(cap_variant.pk))
        self.assertEqual(r.status_code, 400)
        self.assertIn("variant", r.data)

    def test_two_variants_are_separate_lines_and_both_sell(self):
        self.add(quantity=1)
        self.add(quantity=2, variant=str(self.large.pk))
        r = self.shopper.get(f"{self.base}/cart/")
        self.assertEqual(len(r.data["items"]), 2)

        cid = self.shopper.post(
            f"{self.base}/checkout/", {"email": "a@b.com", "shipping_address": ADDRESS}, format="json"
        ).data["id"]
        order = self.place(cid)
        self.assertEqual(order.status_code, 201, order.data)
        self.assertEqual(order.data["total"], "1597.00")
        self.assertEqual(sellable(self.variant), 9)
        self.assertEqual(sellable(self.large), 1)

    def test_continue_selling_allows_going_past_stock(self):
        self.variant.continue_selling = True
        self.variant.save()
        self.assertEqual(self.add(quantity=25).status_code, 201)

    def test_last_unit_goes_to_first_buyer(self):
        set_available(self.variant, self.location, 1)
        second = APIClient()
        second.credentials(HTTP_X_CART_SESSION="sess-2-aaaaaaaaaaaa")

        first_cid = self.checkout(quantity=1, email="a@b.com", shipping_address=ADDRESS)
        second_cid = self.checkout(quantity=1, client=second, email="c@d.com", shipping_address=ADDRESS)

        self.assertEqual(self.place(first_cid).status_code, 201)
        self.assertEqual(self.place(second_cid, client=second).status_code, 400)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(sellable(self.variant), 0)

    def test_two_shoppers_pass_the_stock_check_for_the_last_unit_and_only_one_gets_it(self):
        """Both read "1 left" before either order exists; the lock decides, and the loser sees the shortage message."""
        set_available(self.variant, self.location, 1)
        second = APIClient()
        second.credentials(HTTP_X_CART_SESSION="sess-2-aaaaaaaaaaaa")
        first_cid = self.checkout(quantity=1, email="a@b.com", shipping_address=ADDRESS)
        second_cid = self.checkout(quantity=1, client=second, email="c@d.com", shipping_address=ADDRESS)

        with mock.patch.object(services, "_check_stock"):  # the early read check saw 1 left for both shoppers
            winner = self.place(first_cid)
            loser = self.place(second_cid, client=second)

        self.assertEqual(winner.status_code, 201, winner.data)
        self.assertEqual(loser.status_code, 400)
        self.assertIn("Only 0 left of TEE-1", str(loser.data))
        self.assertEqual(Order.objects.count(), 1)
        item = InventoryItem.objects.get(variant=self.variant)
        self.assertEqual((item.available, item.committed, item.on_hand), (0, 1, 1))
        self.assertEqual(Checkout.objects.get(pk=second_cid).status, "open")  # the loser can try again later

    def test_price_change_after_checkout_asks_shopper_to_confirm_new_total(self):
        cid = self.checkout(quantity=1, email="a@b.com", shipping_address=ADDRESS)
        self.variant.price = Decimal("999.00")
        self.variant.save()

        refused = self.place(cid)
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(sellable(self.variant), 10)  # nothing was committed

        # The checkout now shows the new price; confirming again places the order
        self.assertEqual(self.shopper.get(f"{self.base}/checkout/{cid}/").json()["subtotal"], "999.00")
        order = self.place(cid)
        self.assertEqual(order.status_code, 201)
        self.assertEqual(order.data["subtotal"], "999.00")


class CartAndCheckoutEditingTests(CommerceIntegrationBase):
    def make_code(self, code="SAVE10", value="10"):
        discount = Discount.objects.create(
            store=self.store, name="Ten off", discount_type="percentage", value=Decimal(value),
            starts_at=timezone.now() - timedelta(days=1), is_active=True, is_deleted=False,
        )
        DiscountCode.objects.create(discount=discount, code=code, is_active=True, is_deleted=False)

    def test_delete_empties_the_cart(self):
        self.add(quantity=2)
        r = self.shopper.delete(f"{self.base}/cart/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["items"], [])
        self.assertEqual(self.shopper.post(f"{self.base}/checkout/", {}, format="json").status_code, 400)

    def test_quantity_zero_removes_line(self):
        item_id = self.add(quantity=2).data["items"][0]["id"]
        r = self.shopper.patch(f"{self.base}/cart/items/{item_id}/", {"quantity": 0}, format="json")
        self.assertEqual(r.data["items"], [])
        gone = self.shopper.patch(f"{self.base}/cart/items/{item_id}/", {"quantity": 1}, format="json")
        self.assertEqual(gone.status_code, 400)

    def test_delete_removes_line(self):
        item_id = self.add(quantity=2).data["items"][0]["id"]
        r = self.shopper.delete(f"{self.base}/cart/items/{item_id}/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["items"], [])
        self.assertEqual(self.shopper.delete(f"{self.base}/cart/items/{item_id}/").status_code, 400)

    def test_invalid_input_is_rejected(self):
        self.assertEqual(self.add(quantity=0).status_code, 400)
        bad = self.shopper.post(f"{self.base}/cart/items/", {"product": "not-a-uuid"}, format="json")
        self.assertEqual(bad.status_code, 400)
        email = self.shopper.post(f"{self.base}/checkout/", {"email": "nope"}, format="json")
        self.assertEqual(email.status_code, 400)

    def test_discount_can_be_added_and_removed_on_an_open_checkout(self):
        self.make_code()
        cid = self.checkout(email="a@b.com", shipping_address=ADDRESS)
        url = f"{self.base}/checkout/{cid}/"

        r = self.shopper.patch(url, {"discount_code": "SAVE10"}, format="json")
        self.assertEqual(r.data["total_amount"], "898.20")

        r = self.shopper.patch(url, {"discount_code": ""}, format="json")
        self.assertEqual(r.data["discount_amount"], "0.00")
        self.assertEqual(r.data["total_amount"], "998.00")

    def test_restarting_checkout_after_cart_change_reprices_and_reuses_it(self):
        cid = self.checkout(quantity=1, email="a@b.com")
        self.add(quantity=1)
        r = self.shopper.post(f"{self.base}/checkout/", {}, format="json")
        self.assertEqual(r.data["id"], cid)
        self.assertEqual(r.data["total_amount"], "998.00")
        self.assertEqual(len(r.data["items"]), 1)
        self.assertEqual(r.data["email"], "a@b.com")  # earlier details survive
        self.assertEqual(Checkout.objects.count(), 1)

    def test_completed_checkout_cannot_be_edited_or_cart_reused(self):
        cid = self.checkout(email="a@b.com", shipping_address=ADDRESS)
        self.place(cid)
        r = self.shopper.patch(f"{self.base}/checkout/{cid}/", {"email": "x@y.com"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Checkout.objects.get().email, "a@b.com")

    def test_cancelled_order_returns_stock_and_stays_in_history(self):
        cid = self.checkout(email="a@b.com", shipping_address=ADDRESS)
        order = self.place(cid).data
        self.assertEqual(sellable(self.variant), 8)

        r = self.shopper.post(f"{self.base}/orders/{order['id']}/cancel/")
        self.assertEqual(r.data["status"], "cancelled")
        self.assertEqual(sellable(self.variant), 10)
        listed = self.shopper.get(f"{self.base}/orders/")
        self.assertEqual([o["status"] for o in listed.data], ["cancelled"])


class AbandonedCheckoutTests(CommerceIntegrationBase):
    def test_idle_checkout_is_flagged_for_staff_and_recovers_on_purchase(self):
        cid = self.checkout(email="a@b.com", shipping_address=ADDRESS)
        Checkout.objects.filter(pk=cid).update(updated_at=timezone.now() - timedelta(hours=5))

        self.assertEqual(self.staff.get("/api/v1/checkouts/?abandoned=true").json(), [])
        self.assertEqual(services.mark_abandoned(self.store, timedelta(hours=2)), 1)
        self.assertEqual(services.mark_abandoned(self.store, timedelta(hours=2)), 0)  # only flagged once

        flagged = self.staff.get("/api/v1/checkouts/?abandoned=true").json()
        self.assertEqual([c["id"] for c in flagged], [cid])
        self.assertEqual(flagged[0]["recovery_status"], "pending")

        self.assertEqual(self.place(cid).status_code, 201)
        recovered = self.staff.get(f"/api/v1/checkouts/{cid}/").json()
        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(recovered["recovery_status"], "recovered")
        self.assertIsNotNone(recovered["recovered_at"])

    def test_recent_checkout_is_not_flagged(self):
        self.checkout(email="a@b.com", shipping_address=ADDRESS)
        self.assertEqual(services.mark_abandoned(self.store, timedelta(hours=2)), 0)

    def test_completed_checkout_is_never_flagged(self):
        cid = self.checkout(email="a@b.com", shipping_address=ADDRESS)
        self.place(cid)
        Checkout.objects.filter(pk=cid).update(updated_at=timezone.now() - timedelta(days=2))
        self.assertEqual(services.mark_abandoned(self.store, timedelta(hours=2)), 0)


class PricingAndPaymentTests(CommerceIntegrationBase):
    def configure(self, **settings):
        r = self.staff.patch(f"/api/v1/stores/{self.store.pk}/", settings, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_staff_set_tax_and_shipping_and_shopper_checkout_uses_them(self):
        saved = self.configure(taxRate="18", shippingFlatRate="50", freeShippingThreshold="2000")
        self.assertEqual((saved["taxRate"], saved["shippingFlatRate"]), ("18.00", "50.00"))

        cid = self.checkout(quantity=2, email="a@b.com", shipping_address=ADDRESS)
        detail = self.shopper.get(f"{self.base}/checkout/{cid}/").data
        self.assertEqual(
            (detail["subtotal"], detail["tax_amount"], detail["shipping_amount"], detail["total_amount"]),
            ("998.00", "179.64", "50.00", "1227.64"),
        )

        order = self.place(cid)
        self.assertEqual((order.data["tax_amount"], order.data["shipping_amount"], order.data["total"]),
                         ("179.64", "50.00", "1227.64"))

    def test_free_shipping_threshold_waives_shipping(self):
        self.configure(taxRate="10", shippingFlatRate="50", freeShippingThreshold="900")
        cid = self.checkout(quantity=2, email="a@b.com", shipping_address=ADDRESS)
        detail = self.shopper.get(f"{self.base}/checkout/{cid}/").data
        self.assertEqual((detail["shipping_amount"], detail["tax_amount"]), ("0.00", "99.80"))

    def test_invalid_store_pricing_is_rejected(self):
        for bad in ({"taxRate": "101"}, {"taxRate": "-1"}, {"shippingFlatRate": "-5"}):
            r = self.staff.patch(f"/api/v1/stores/{self.store.pk}/", bad, format="json")
            self.assertEqual(r.status_code, 400, bad)

    def test_cod_order_leaves_pending_payment_staff_can_see(self):
        cid = self.checkout(email="a@b.com", shipping_address=ADDRESS)
        order = self.place(cid)
        self.assertEqual(order.status_code, 201)

        payments = self.staff.get(f"/api/v1/payments/?order={order.data['id']}").json()
        self.assertEqual(len(payments), 1)
        self.assertEqual((payments[0]["status"], payments[0]["provider"]), ("pending", "cod"))
        self.assertEqual(Decimal(payments[0]["amount"]), Decimal(order.data["total"]))

        # Repeating the call returns the same order and no second payment
        again = self.place(cid)
        self.assertEqual(again.data["id"], order.data["id"])
        self.assertEqual(len(self.staff.get(f"/api/v1/payments/?order={order.data['id']}").json()), 1)

    def test_pending_cod_payment_is_not_counted_as_paid_and_cannot_be_refunded(self):
        order = self.place(self.checkout(email="a@b.com", shipping_address=ADDRESS)).data
        payment = self.staff.get(f"/api/v1/payments/?order={order['id']}").json()[0]

        refund = self.staff.post(f"/api/v1/payments/{payment['id']}/refund/", {"amount": "1.00"}, format="json")
        self.assertEqual(refund.status_code, 400)

        # Cash isn't in yet, so a shopper may still cancel
        self.assertEqual(self.shopper.post(f"{self.base}/orders/{order['id']}/cancel/").status_code, 200)

    def test_capturing_cod_payment_marks_it_received_and_blocks_shopper_cancel(self):
        order = self.place(self.checkout(email="a@b.com", shipping_address=ADDRESS)).data
        payment = self.staff.get(f"/api/v1/payments/?order={order['id']}").json()[0]

        captured = self.staff.post(f"/api/v1/payments/{payment['id']}/capture/", {"reference": "CASH-1"}, format="json")
        self.assertEqual((captured.status_code, captured.data["status"]), (200, "captured"))
        self.assertEqual(self.staff.post(f"/api/v1/payments/{payment['id']}/capture/").status_code, 400)

        # Nothing left to collect
        extra = self.staff.post("/api/v1/payments/", {"order": order["id"], "amount": "1.00"}, format="json")
        self.assertEqual(extra.status_code, 400)

        # Money received: the shopper can't cancel; staff can still refund
        self.assertEqual(self.shopper.post(f"{self.base}/orders/{order['id']}/cancel/").status_code, 400)
        refund = self.staff.post(f"/api/v1/payments/{payment['id']}/refund/", {"amount": "1.00"}, format="json")
        self.assertEqual(refund.status_code, 200)

    def test_bad_order_filter_on_payments_is_a_400(self):
        self.assertEqual(self.staff.get("/api/v1/payments/?order=abc").status_code, 400)

    def test_unknown_payment_method_is_rejected_and_nothing_is_ordered(self):
        cid = self.checkout(email="a@b.com", shipping_address=ADDRESS)
        r = self.shopper.post(f"{self.base}/checkout/{cid}/complete/", {"payment_method": "bitcoin"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(sellable(self.variant), 10)

    def test_guest_order_creates_one_customer_reused_next_time(self):
        self.place(self.checkout(email="Guest@Example.com", shipping_address=ADDRESS))
        other = APIClient()
        other.credentials(HTTP_X_CART_SESSION="sess-2-aaaaaaaaaaaa")
        self.place(self.checkout(quantity=1, client=other, email="guest@example.com", shipping_address=ADDRESS), client=other)
        orders = list(Order.objects.all())
        self.assertEqual(len(orders), 2)
        self.assertEqual(orders[0].customer_id, orders[1].customer_id)

    def test_price_change_shows_in_cart_and_checkout(self):
        self.add(quantity=1)
        self.variant.price = Decimal("599.00")
        self.variant.save()
        cart = self.shopper.get(f"{self.base}/cart/").data
        self.assertEqual(cart["items"][0]["unit_price"], "599.00")
        r = self.shopper.post(f"{self.base}/checkout/", {"email": "a@b.com", "shipping_address": ADDRESS}, format="json")
        self.assertEqual(r.data["subtotal"], "599.00")
        self.assertEqual(r.data["email"], "a@b.com")  # response reflects the saved details
    def test_tax_rates_are_isolated_between_stores(self):
        # Store 1 uses a 5% tax rate.
        self.configure(taxRate="5", shippingFlatRate="0")
        first_checkout_id = self.checkout(
            quantity=2,
            email="first-shopper@example.com",
            shipping_address=ADDRESS,
        )
        first_detail = self.shopper.get(
            f"{self.base}/checkout/{first_checkout_id}/"
        ).data
        self.assertEqual(first_detail["tax_amount"], "49.90")

        # Create a separate store with an 18% tax rate.
        _, second_store = sign_up_with_store("second-owner@example.com")
        second_store.status = "active"
        second_store.tax_rate = Decimal("18.00")
        second_store.shipping_flat_rate = Decimal("0.00")
        second_store.save(
            update_fields=[
                "status",
                "tax_rate",
                "shipping_flat_rate",
            ]
        )

        second_location = Location.objects.create(
            store=second_store,
            name="Second warehouse",
        )
        second_product, _ = stock_product(
            second_store,
            "Second Store Tee",
            "TEE-STORE-2",
            "499.00",
            10,
            second_location,
        )

        # Use a separate shopper session and the second store's storefront.
        second_shopper = APIClient()
        second_shopper.credentials(
            HTTP_X_CART_SESSION="sess-2-bbbbbbbbbbbb"
        )
        second_base = f"/api/v1/storefront/{second_store.store_slug}"

        added = second_shopper.post(
            f"{second_base}/cart/items/",
            {
                "product": str(second_product.pk),
                "quantity": 2,
            },
            format="json",
        )
        self.assertEqual(added.status_code, 201, added.data)

        response = second_shopper.post(
            f"{second_base}/checkout/",
            {
                "email": "second-shopper@example.com",
                "shipping_address": ADDRESS,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)

        second_checkout_id = response.data["id"]
        second_detail = second_shopper.get(
            f"{second_base}/checkout/{second_checkout_id}/"
        ).data
        self.assertEqual(second_detail["tax_amount"], "179.64")

        # The second store's rate must not change the first store's result.
        first_detail_again = self.shopper.get(
            f"{self.base}/checkout/{first_checkout_id}/"
        ).data
        self.assertEqual(first_detail_again["tax_amount"], "49.90")

class SweepCartsTests(CommerceIntegrationBase):
    def test_command_abandons_idle_checkouts_and_expires_idle_carts(self):
        from django.core.management import call_command

        cid = self.checkout(email="a@b.com", shipping_address=ADDRESS)
        idle = APIClient()
        idle.credentials(HTTP_X_CART_SESSION="sess-idle-aaaaaaaaaaa")
        idle.get(f"{self.base}/cart/")

        Checkout.objects.update(updated_at=timezone.now() - timedelta(hours=3))
        Cart.objects.filter(session_id="sess-idle-aaaaaaaaaaa").update(updated_at=timezone.now() - timedelta(days=45))
        call_command("sweep_carts", stdout=StringIO())

        self.assertEqual([c["id"] for c in self.staff.get("/api/v1/checkouts/?abandoned=true").json()], [cid])
        self.assertEqual(self.staff.get("/api/v1/carts/?status=expired").json()[0]["session_id"], "sess-idle-aaaaaaaaaaa")
        # the cart with a checkout in progress is kept open so the shopper can still buy
        self.assertEqual(self.place(cid).status_code, 201)


@unittest.skipUnless(connection.vendor == "postgresql", "needs PostgreSQL: SQLite ignores row locks")
class ConcurrentStockTests(TransactionTestCase):
    """
    Real threads against real row locks. Skipped on SQLite. Run it only on a LOCAL PostgreSQL
    (see the commands given with this change): it writes real rows into the test database.
    """

    def setUp(self):
        cache.clear()
        self.store = Store.objects.create(store_name="Race Shop", store_slug="race-shop", status="active")
        self.location = Location.objects.create(store=self.store, name="Main warehouse")

    def race(self, jobs):
        """Run each job in its own thread, all released at the same moment: [("ok", value) | ("refused", text) | ("error", text)]."""
        barrier = threading.Barrier(len(jobs))
        results = [None] * len(jobs)

        def run(index, job):
            try:
                barrier.wait(timeout=30)
                results[index] = ("ok", job())
            except ValidationError as exc:
                results[index] = ("refused", str(exc.detail))
            except Exception as exc:  # a deadlock or a SQL error ends up here
                results[index] = ("error", repr(exc))
            finally:
                connection.close()

        threads = [threading.Thread(target=run, args=(i, job)) for i, job in enumerate(jobs)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        return results

    def test_two_shoppers_race_for_the_last_unit_and_only_one_gets_it(self):
        product, variant = stock_product(self.store, "Tee", "TEE-1", "499.00", 1, self.location)
        checkouts = []
        for n in (1, 2):
            cart = services.get_or_create_cart(self.store, session_id=f"race-session-{n}-aaaaaaaa")
            services.add_item(cart, product, 1, variant)
            checkouts.append(services.start_checkout(cart, email=f"s{n}@example.com", shipping_address=ADDRESS))

        results = self.race([lambda checkout=checkout: services.place_order(checkout) for checkout in checkouts])

        self.assertEqual(sorted(kind for kind, _ in results), ["ok", "refused"], results)
        refused = next(text for kind, text in results if kind == "refused")
        self.assertTrue("left" in refused or "sold out" in refused, refused)  # the lock-time or the early message
        item = InventoryItem.objects.get(variant=variant)
        self.assertEqual((item.available, item.committed, item.on_hand), (0, 1, 1))
        self.assertEqual(Order.objects.count(), 1)

    def test_two_orders_with_the_same_products_in_opposite_order_do_not_deadlock(self):
        tee, tee_variant = stock_product(self.store, "Tee", "TEE-1", "499.00", 50, self.location)
        hat, hat_variant = stock_product(self.store, "Hat", "HAT-1", "299.00", 50, self.location)

        def order(first, second):
            def place():
                return orders_services.create_order(self.store, [
                    {"product": first[0], "variant": first[1], "quantity": 1},
                    {"product": second[0], "variant": second[1], "quantity": 1},
                ])
            return place

        for _ in range(10):
            results = self.race([order((tee, tee_variant), (hat, hat_variant)), order((hat, hat_variant), (tee, tee_variant))])
            self.assertEqual([kind for kind, _ in results], ["ok", "ok"], results)

        for variant in (tee_variant, hat_variant):
            item = InventoryItem.objects.get(variant=variant)
            self.assertEqual((item.available, item.committed), (30, 20))
        self.assertEqual(Order.objects.count(), 20)


class CustomerCancelWindowTests(CommerceIntegrationBase):
    """A shopper can cancel their own order within the store\'s cancellation window (default 60 minutes); staff have no limit."""

    NEW_MESSAGE = "Orders can only be cancelled within 1 hour of ordering. Please contact the store."

    def placed(self, minutes_ago=0):
        order = self.place(self.checkout(email="a@b.com", shipping_address=ADDRESS)).data
        if minutes_ago:
            Order.objects.filter(pk=order["id"]).update(created_at=timezone.now() - timedelta(minutes=minutes_ago))
        return order

    def cancel(self, order):
        return self.shopper.post(f"{self.base}/orders/{order['id']}/cancel/")

    def receive_money(self, order):
        from apps.payments import services as payments
        from apps.payments.models import Payment

        payments.capture_payment(Payment.objects.get(order=order["id"]))

    def test_within_the_hour_the_shopper_can_cancel_as_before(self):
        order = self.placed(minutes_ago=59)
        r = self.cancel(order)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["status"], "cancelled")
        self.assertEqual(sellable(self.variant), 10)

    def test_after_the_hour_the_shopper_is_refused_with_the_new_message_and_nothing_changes(self):
        order = self.placed(minutes_ago=61)
        r = self.cancel(order)
        self.assertEqual(r.status_code, 400)
        self.assertIn(self.NEW_MESSAGE, str(r.data))
        self.assertEqual(Order.objects.get(pk=order["id"]).status, "open")
        self.assertEqual(sellable(self.variant), 8)  # still committed

    def test_a_paid_order_inside_the_hour_still_gets_the_existing_message(self):
        order = self.placed(minutes_ago=5)
        self.receive_money(order)
        r = self.cancel(order)
        self.assertEqual(r.status_code, 400)
        self.assertIn("This order has been paid. Please contact the store to cancel it.", str(r.data))
        self.assertNotIn("within 1 hour", str(r.data))

    def test_a_paid_order_after_the_hour_gets_the_new_message(self):
        order = self.placed(minutes_ago=90)
        self.receive_money(order)
        r = self.cancel(order)
        self.assertEqual(r.status_code, 400)
        self.assertIn(self.NEW_MESSAGE, str(r.data))

    def test_staff_can_cancel_after_the_hour(self):
        order = self.placed(minutes_ago=300)
        r = self.staff.post(f"/api/v1/orders/{order['id']}/cancel/")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.json()["status"], "cancelled")
        self.assertEqual(sellable(self.variant), 10)

    def test_staff_still_cannot_cancel_once_items_have_shipped(self):
        order = self.placed(minutes_ago=300)
        Order.objects.filter(pk=order["id"]).update(fulfillment_status="partial")
        r = self.staff.post(f"/api/v1/orders/{order['id']}/cancel/")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Orders with shipped items can't be cancelled.", str(r.json()))

    def set_rules(self, **fields):
        type(self.store).objects.filter(pk=self.store.pk).update(**fields)

    def test_the_window_is_a_store_rule_and_the_message_follows_it(self):
        order = self.placed(minutes_ago=100)
        self.set_rules(cancellation_window_minutes=30)
        r = self.cancel(order)
        self.assertEqual(r.status_code, 400)
        self.assertIn("within 30 minutes of ordering", str(r.data))
        self.set_rules(cancellation_window_minutes=120)
        self.assertEqual(self.cancel(order).status_code, 200)
        other = self.placed(minutes_ago=100)
        self.set_rules(cancellation_window_minutes=60 * 24)
        self.assertEqual(self.cancel(other).status_code, 200)
        self.store.refresh_from_db()
        self.assertEqual(orders_services.customer_cancel_window_text(self.store), "1 day")
        self.set_rules(cancellation_window_minutes=120)
        self.store.refresh_from_db()
        self.assertEqual(orders_services.customer_cancel_window_text(self.store), "2 hours")

    def test_a_store_with_cancellations_off_refuses_shoppers_but_not_staff(self):
        self.set_rules(cancellations_enabled=False)
        order = self.placed()
        r = self.cancel(order)
        self.assertEqual(r.status_code, 400)
        self.assertIn("doesn't accept cancellations online", str(r.data))
        self.assertIsNone(self.shopper.get(f"{self.base}/orders/{order['id']}/").data["customer_cancel_until"])
        self.assertEqual(self.staff.post(f"/api/v1/orders/{order['id']}/cancel/").status_code, 200)

    def test_the_order_tells_the_storefront_until_when_it_can_be_cancelled(self):
        order = self.placed()
        created = Order.objects.get(pk=order["id"]).created_at
        until = self.shopper.get(f"{self.base}/orders/{order['id']}/").data["customer_cancel_until"]
        self.assertIsNotNone(until)
        from django.utils.dateparse import parse_datetime

        self.assertEqual(parse_datetime(until), created + timedelta(minutes=60))

    def test_it_is_empty_when_the_shopper_can_not_cancel(self):
        late = self.placed(minutes_ago=61)
        self.assertIsNone(self.shopper.get(f"{self.base}/orders/{late['id']}/").data["customer_cancel_until"])

        paid = self.placed()
        self.receive_money(paid)
        self.assertIsNone(self.shopper.get(f"{self.base}/orders/{paid['id']}/").data["customer_cancel_until"])

        shipped = self.placed()
        Order.objects.filter(pk=shipped["id"]).update(fulfillment_status="partial")
        self.assertIsNone(self.shopper.get(f"{self.base}/orders/{shipped['id']}/").data["customer_cancel_until"])

        cancelled = self.placed()
        self.cancel(cancelled)
        self.assertIsNone(self.shopper.get(f"{self.base}/orders/{cancelled['id']}/").data["customer_cancel_until"])


class OrderReturnTests(CommerceIntegrationBase):
    """Staff mark shipped items as returned: the stock comes back, each unit once."""

    def placed(self, quantity=2):
        order = self.place(self.checkout(quantity=quantity, email="a@b.com", shipping_address=ADDRESS)).data
        return order, order["items"][0]["id"]

    def ship(self, order, quantity=None):
        body = {"order": order["id"]}
        if quantity:
            body["items"] = [{"order_item": order["items"][0]["id"], "quantity": quantity}]
        r = self.staff.post("/api/v1/fulfillments/", body, format="json")
        self.assertEqual(r.status_code, 201, r.content)

    def give_back(self, order, item, quantity, expect=200, **extra):
        r = self.staff.post(
            f"/api/v1/orders/{order['id']}/return/",
            {"items": [{"order_item": item, "quantity": quantity, **extra}]}, format="json",
        )
        self.assertEqual(r.status_code, expect, r.content)
        return r.json()

    def receive_money(self, order):
        from apps.payments import services as payments
        from apps.payments.models import Payment

        payments.capture_payment(Payment.objects.get(order=order["id"]))

    def stock(self):
        item = InventoryItem.objects.get(variant=self.variant)
        return item.available, item.committed

    def test_a_failed_cash_on_delivery_order_comes_back_cancelled_with_its_stock(self):
        from apps.inventory.models import InventoryHistory
        from apps.payments.models import Payment

        order, item = self.placed()
        self.ship(order)
        self.assertEqual(self.stock(), (8, 0))

        body = self.give_back(order, item, 2)

        self.assertEqual(self.stock(), (10, 0))
        self.assertEqual(body["status"], "cancelled")
        self.assertIsNotNone(body["returned_at"])
        self.assertEqual(body["items"][0]["returned_quantity"], 2)
        self.assertEqual(Payment.objects.get(order=order["id"]).status, "cancelled")  # the pending payment is voided
        row = InventoryHistory.objects.get(reason="refund_restock")
        self.assertEqual((row.source, row.reference_id), ("return", order["id"]))

    def test_the_same_units_cannot_be_returned_twice(self):
        order, item = self.placed()
        self.ship(order)
        self.give_back(order, item, 2)

        again = self.give_back(order, item, 1, expect=400)  # the order is cancelled now
        self.assertIn("cancelled", str(again))
        self.assertEqual(self.stock(), (10, 0))

    def test_a_partial_return_keeps_the_order_open_and_the_rest_can_follow_once(self):
        order, item = self.placed()
        self.ship(order)

        body = self.give_back(order, item, 1)
        self.assertEqual((body["status"], body["returned_at"]), ("open", None))
        self.assertEqual(self.stock(), (9, 0))

        too_many = self.give_back(order, item, 2, expect=400)
        self.assertIn("Only 1 of Tee can still be returned.", str(too_many))
        self.assertEqual(self.stock(), (9, 0))

        body = self.give_back(order, item, 1)
        self.assertEqual((body["status"], self.stock()), ("cancelled", (10, 0)))

    def test_units_that_have_not_shipped_cannot_be_returned(self):
        order, item = self.placed()
        refused = self.give_back(order, item, 1, expect=400)
        self.assertIn("has not shipped", str(refused))
        self.ship(order, quantity=1)
        self.give_back(order, item, 2, expect=400)  # only one shipped
        self.assertEqual(self.stock(), (8, 1))

    def test_a_paid_order_keeps_its_status_and_is_flagged_and_the_refund_stays_with_staff(self):
        from apps.payments.models import Payment

        order, item = self.placed()
        self.receive_money(order)
        self.ship(order)

        body = self.give_back(order, item, 2)

        self.assertEqual(self.stock(), (10, 0))
        self.assertEqual(body["status"], "open")
        self.assertIsNotNone(body["returned_at"])
        self.assertEqual(Payment.objects.get(order=order["id"]).status, "captured")  # no automatic refund

    def test_refunding_after_the_return_does_not_put_the_stock_back_a_second_time(self):
        from apps.payments import services as payments
        from apps.payments.models import Payment

        order, item = self.placed()
        self.receive_money(order)
        self.ship(order)
        self.give_back(order, item, 2)

        payments.refund(Payment.objects.get(order=order["id"]), Decimal("998.00"))

        self.assertEqual(self.stock(), (10, 0))  # not 12
        self.assertEqual(Order.objects.get(pk=order["id"]).status, "open")

    def test_returning_after_a_full_refund_puts_the_shipped_units_back_once(self):
        from apps.payments import services as payments
        from apps.payments.models import Payment

        order, item = self.placed()
        self.receive_money(order)
        self.ship(order)
        payments.refund(Payment.objects.get(order=order["id"]), Decimal("998.00"))
        self.assertEqual(self.stock(), (8, 0))  # a refund alone does not restock shipped goods

        self.give_back(order, item, 2)

        self.assertEqual(self.stock(), (10, 0))
        self.give_back(order, item, 1, expect=400)

    def test_a_partly_shipped_unpaid_order_returned_in_full_is_cancelled_and_its_unshipped_unit_released(self):
        order, item = self.placed()
        self.ship(order, quantity=1)
        self.assertEqual(self.stock(), (8, 1))

        body = self.give_back(order, item, 1)

        self.assertEqual(body["status"], "cancelled")
        self.assertEqual(self.stock(), (10, 0))

    def test_staff_can_choose_the_location_and_only_one_of_their_own_store(self):
        elsewhere = Location.objects.create(store=self.store, name="Shop floor")
        order, item = self.placed()
        self.ship(order)

        self.give_back(order, item, 1, location=str(elsewhere.pk))

        self.assertEqual(InventoryItem.objects.get(variant=self.variant, location=elsewhere).available, 1)
        self.assertEqual(InventoryItem.objects.get(variant=self.variant, location=self.location).available, 8)

        other_client, other_store = sign_up_with_store("returns-other@example.com")
        foreign = Location.objects.create(store=other_store, name="Not ours")
        refused = self.give_back(order, item, 1, expect=400, location=str(foreign.pk))
        self.assertIn("That location isn't in this store.", str(refused))

    def test_another_stores_order_cannot_be_returned(self):
        order, item = self.placed()
        self.ship(order)
        other_client, _ = sign_up_with_store("returns-rival@example.com")
        r = other_client.post(
            f"/api/v1/orders/{order['id']}/return/", {"items": [{"order_item": item, "quantity": 1}]}, format="json"
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.stock(), (8, 0))

    def test_nothing_to_return_and_bad_input_are_refused(self):
        order, item = self.placed()
        self.ship(order)
        r = self.staff.post(f"/api/v1/orders/{order['id']}/return/", {"items": []}, format="json")
        self.assertEqual(r.status_code, 400)
        r = self.staff.post(
            f"/api/v1/orders/{order['id']}/return/", {"items": [{"order_item": item, "quantity": 0}]}, format="json"
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.stock(), (8, 0))


class StockAndPaymentJourneyTests(CommerceIntegrationBase):
    """
    Whole cash-on-delivery journeys. After EVERY step the stock rules hold (on_hand = available + committed +
    unavailable, nothing negative, a history row for every change) and the payment says what it should.
    """

    def second_shopper(self):
        shopper = APIClient()
        shopper.credentials(HTTP_X_CART_SESSION="sess-2-aaaaaaaaaaaa")
        return shopper

    def consistent(self, store=None):
        return assert_stock_consistent(self, store or self.store)

    def stock(self, variant=None):
        item = InventoryItem.objects.get(variant=variant or self.variant)
        return item.available, item.committed

    def place_cod(self, quantity=2, client=None):
        cid = self.checkout(quantity=quantity, client=client, email="a@b.com", shipping_address=ADDRESS)
        r = self.place(cid, client=client)
        self.assertEqual(r.status_code, 201, r.data)
        return r.data

    def payment(self, order):
        return Payment.objects.get(order=order["id"])

    def capture(self, order):
        r = self.staff.post(f"/api/v1/payments/{self.payment(order).pk}/capture/", {"reference": "CASH"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)

    def ship(self, order, quantity=None):
        body = {"order": order["id"]}
        if quantity:
            body["items"] = [{"order_item": order["items"][0]["id"], "quantity": quantity}]
        r = self.staff.post("/api/v1/fulfillments/", body, format="json")
        self.assertEqual(r.status_code, 201, r.content)

    def give_back(self, order, quantity):
        r = self.staff.post(
            f"/api/v1/orders/{order['id']}/return/",
            {"items": [{"order_item": order["items"][0]["id"], "quantity": quantity}]}, format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_1_cod_order_placed_then_paid_then_fulfilled(self):
        order = self.place_cod()
        self.assertEqual(self.stock(), (8, 2))
        history = self.consistent()
        self.assertEqual((history["order_placed"], history["order_fulfilled"]), (1, 0))
        self.assertEqual(self.payment(order).status, "pending")

        self.capture(order)
        self.assertEqual(self.payment(order).status, "captured")
        self.assertEqual(self.stock(), (8, 2))  # money received does not move stock
        self.consistent()

        self.ship(order)
        self.assertEqual(self.stock(), (8, 0))  # committed goes down, available does not come back
        self.assertEqual(Order.objects.get(pk=order["id"]).fulfillment_status, "fulfilled")
        history = self.consistent()
        self.assertEqual((history["order_placed"], history["order_fulfilled"], history["order_cancelled"]), (1, 1, 0))
        self.assertEqual(self.payment(order).status, "captured")

    def test_2_cod_order_cancelled_before_shipping_by_the_shopper_and_by_staff(self):
        by_shopper = self.place_cod()
        self.assertEqual(self.shopper.post(f"{self.base}/orders/{by_shopper['id']}/cancel/").status_code, 200)
        self.assertEqual(self.stock(), (10, 0))
        self.assertEqual(self.payment(by_shopper).status, "cancelled")  # the pending payment is voided
        history = self.consistent()
        self.assertEqual((history["order_placed"], history["order_cancelled"]), (1, 1))

        other = self.second_shopper()
        by_staff = self.place_cod(client=other)
        self.assertEqual(self.stock(), (8, 2))
        self.consistent()
        r = self.staff.post(f"/api/v1/orders/{by_staff['id']}/cancel/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.stock(), (10, 0))
        self.assertEqual(self.payment(by_staff).status, "cancelled")
        history = self.consistent()
        self.assertEqual((history["order_placed"], history["order_cancelled"]), (2, 2))

    def test_3_cod_delivery_fails_and_the_returned_items_cancel_the_unpaid_order(self):
        order = self.place_cod()
        self.consistent()
        self.ship(order)
        self.assertEqual(self.stock(), (8, 0))
        self.consistent()

        body = self.give_back(order, 2)

        self.assertEqual(self.stock(), (10, 0))  # back once
        self.assertEqual((body["status"], self.payment(order).status), ("cancelled", "cancelled"))
        history = self.consistent()
        self.assertEqual((history["order_placed"], history["order_fulfilled"], history["refund_restock"]), (1, 1, 1))
        self.assertEqual(history["order_cancelled"], 0)  # nothing was left committed to release

    def test_9_a_paid_order_that_is_shipped_and_returned_is_flagged_and_stock_comes_back_once(self):
        order = self.place_cod()
        self.capture(order)
        self.ship(order)
        self.consistent()

        body = self.give_back(order, 2)

        self.assertEqual(self.stock(), (10, 0))
        self.assertEqual((body["status"], self.payment(order).status), ("open", "captured"))
        self.assertIsNotNone(body["returned_at"])
        history = self.consistent()
        self.assertEqual(history["refund_restock"], 1)
        r = self.staff.post(
            f"/api/v1/orders/{order['id']}/return/",
            {"items": [{"order_item": order["items"][0]["id"], "quantity": 1}]}, format="json",
        )
        self.assertEqual(r.status_code, 400)  # the same units cannot come back twice
        self.assertEqual(self.stock(), (10, 0))
        self.assertEqual(self.consistent()["refund_restock"], 1)

    def test_10_the_shopper_can_cancel_within_the_hour_and_not_after_it(self):
        inside = self.place_cod()
        self.assertEqual(self.shopper.post(f"{self.base}/orders/{inside['id']}/cancel/").status_code, 200)
        self.assertEqual(self.stock(), (10, 0))
        before = self.consistent()
        self.assertEqual(before["order_cancelled"], 1)

        other = self.second_shopper()
        late = self.place_cod(client=other)
        Order.objects.filter(pk=late["id"]).update(created_at=timezone.now() - timedelta(minutes=61))
        history = self.consistent()
        r = other.post(f"{self.base}/orders/{late['id']}/cancel/")

        self.assertEqual(r.status_code, 400)
        self.assertIn("within 1 hour", str(r.data))
        self.assertEqual(self.stock(), (8, 2))  # untouched by the refused cancel
        self.assertEqual(self.consistent(), history)  # and no history row was written
        self.assertEqual(self.payment(late).status, "pending")

    def test_11_a_journey_in_one_store_never_touches_the_other_stores_stock_or_payments(self):
        other_client, other_store = sign_up_with_store("journey-b@example.com")
        other_store.status = "active"
        other_store.save()
        other_location = Location.objects.create(store=other_store, name="Main warehouse")
        stock_product(other_store, "Tee", "TEE-1", "499.00", 10, other_location)  # the same SKU as store A's

        def snapshot(store):
            rows = sorted(InventoryItem.objects.filter(variant__product__store=store).values_list(
                "variant__sku", "available", "committed", "unavailable", "on_hand"))
            return (rows, InventoryHistory.objects.filter(store=store).count(),
                    Payment.objects.filter(store=store).count(), Order.objects.filter(store=store).count())

        b_before = snapshot(other_store)

        # Store A: a paid order that is shipped and partly returned, an order cancelled, a paid order cancelled
        paid = self.place_cod(quantity=3)
        self.capture(paid)
        self.ship(paid)
        self.give_back(paid, 1)
        second = self.second_shopper()
        cancelled = self.place_cod(client=second)
        self.assertEqual(second.post(f"{self.base}/orders/{cancelled['id']}/cancel/").status_code, 200)
        refunded = self.place_cod(client=second)
        self.capture(refunded)
        self.assertEqual(self.staff.post(f"/api/v1/orders/{refunded['id']}/cancel/").status_code, 200)

        self.assertEqual(snapshot(other_store), b_before)
        self.assertEqual(self.stock(), (8, 0))  # 10 - 3 shipped + 1 returned
        self.consistent()
        self.consistent(other_store)

        # Store B sells and cancels: store A does not move
        a_before = snapshot(self.store)
        base_b = f"/api/v1/storefront/{other_store.store_slug}"
        shopper_b = APIClient()
        shopper_b.credentials(HTTP_X_CART_SESSION="sess-b-aaaaaaaaaaaaaa")
        product_b = Product.objects.get(store=other_store)
        self.assertEqual(shopper_b.post(f"{base_b}/cart/items/", {"product": str(product_b.pk), "quantity": 2}, format="json").status_code, 201)
        cid = shopper_b.post(f"{base_b}/checkout/", {"email": "b@b.com", "shipping_address": ADDRESS}, format="json").data["id"]
        placed = shopper_b.post(f"{base_b}/checkout/{cid}/complete/")
        self.assertEqual(placed.status_code, 201, placed.data)
        self.assertEqual(other_client.post(f"/api/v1/orders/{placed.data['id']}/cancel/").status_code, 200)

        self.assertEqual(snapshot(self.store), a_before)
        self.consistent()
        history_b = self.consistent(other_store)
        self.assertEqual((history_b["order_placed"], history_b["order_cancelled"]), (1, 1))

    def test_12_a_variant_that_sells_past_its_stock_goes_negative_and_comes_back_exactly(self):
        set_available(self.variant, self.location, 1)
        self.variant.continue_selling = True
        self.variant.save()

        first = self.place_cod(quantity=3)
        self.assertEqual(self.stock(), (-2, 3))  # 1 on the shelf, 3 sold
        self.consistent()
        self.assertEqual(self.shopper.post(f"{self.base}/orders/{first['id']}/cancel/").status_code, 200)
        self.assertEqual(self.stock(), (1, 0))
        self.consistent()

        other = self.second_shopper()
        second = self.place_cod(quantity=3, client=other)
        self.capture(second)
        self.ship(second)
        self.assertEqual(self.stock(), (-2, 0))
        self.consistent()
        self.give_back(second, 3)
        self.assertEqual(self.stock(), (1, 0))  # exactly what it was before
        self.consistent()

        third = self.place_cod(quantity=3, client=self.second_shopper())
        self.capture(third)
        payment = self.payment(third)
        r = self.staff.post(f"/api/v1/payments/{payment.pk}/refund/", {"amount": str(payment.amount)}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(Order.objects.get(pk=third["id"]).status, "cancelled")
        self.assertEqual(self.stock(), (1, 0))
        self.consistent()

    def untracked_admin_order(self, product):
        r = self.staff.post("/api/v1/orders/", {"items": [{"product": str(product.pk), "quantity": 3}]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def pay_in_full(self, order):
        r = self.staff.post("/api/v1/payments/", {"order": order["id"], "amount": order["total"], "provider": "manual"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def untracked_product(self):
        product = Product.objects.create(store=self.store, title="Gift card", status="active")
        variant = ProductVariant.objects.create(product=product, sku="GIFT-1", price=Decimal("50.00"))
        return product, variant

    def test_12_an_untracked_variant_is_never_blocked_and_never_gets_a_stock_row(self):
        product, variant = self.untracked_product()

        cancelled = self.untracked_admin_order(product)  # place and cancel
        self.assertEqual(self.staff.post(f"/api/v1/orders/{cancelled['id']}/cancel/").status_code, 200)
        shipped = self.untracked_admin_order(product)  # place, pay, ship
        self.pay_in_full(shipped)
        self.ship_admin(shipped)
        refunded = self.untracked_admin_order(product)  # place, pay, refund in full: cancelled
        payment = self.pay_in_full(refunded)
        r = self.staff.post(f"/api/v1/payments/{payment['id']}/refund/", {"amount": refunded["total"]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(Order.objects.get(pk=refunded["id"]).status, "cancelled")

        self.assertFalse(InventoryItem.objects.filter(variant=variant).exists())  # still untracked
        self.assertFalse(InventoryHistory.objects.filter(variant=variant).exists())
        self.assertEqual(self.stock(), (10, 0))  # the other product is untouched
        self.consistent()

    def ship_admin(self, order):
        r = self.staff.post("/api/v1/fulfillments/", {"order": order["id"]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)

    def test_12_returning_an_untracked_variants_items_does_not_start_tracking_it(self):
        product, variant = self.untracked_product()
        order = self.untracked_admin_order(product)
        self.pay_in_full(order)
        self.ship_admin(order)

        r = self.staff.post(
            f"/api/v1/orders/{order['id']}/return/",
            {"items": [{"order_item": order["items"][0]["id"], "quantity": 3}]}, format="json",
        )

        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(InventoryItem.objects.filter(variant=variant).exists())  # as before: no stock rows
        self.assertFalse(InventoryHistory.objects.filter(variant=variant).exists())
        self.assertEqual(r.json()["items"][0]["returned_quantity"], 3)  # the units still count as returned
        self.assertIsNotNone(r.json()["returned_at"])
        again = self.staff.post(
            f"/api/v1/orders/{order['id']}/return/",
            {"items": [{"order_item": order["items"][0]["id"], "quantity": 1}]}, format="json",
        )
        self.assertEqual(again.status_code, 400)  # so they cannot be returned a second time
        self.assertFalse(InventoryItem.objects.filter(variant=variant).exists())
        self.assertEqual(self.stock(), (10, 0))
        self.consistent()

    def test_7_a_paid_cod_order_cancelled_by_staff_is_refunded_then_the_stock_comes_back(self):
        order = self.place_cod()
        self.capture(order)
        self.consistent()

        r = self.staff.post(f"/api/v1/orders/{order['id']}/cancel/")

        self.assertEqual(r.status_code, 200, r.content)
        payment = self.payment(order)
        self.assertEqual(payment.status, "refunded")
        self.assertEqual(payment.transactions.filter(transaction_type="refund").count(), 1)
        self.assertEqual(self.stock(), (10, 0))
        history = self.consistent()
        self.assertEqual((history["order_placed"], history["order_cancelled"]), (1, 1))
        again = self.staff.post(f"/api/v1/orders/{order['id']}/cancel/")
        self.assertEqual(again.status_code, 400)  # no second refund, no second release
        self.assertEqual(self.stock(), (10, 0))
        self.assertEqual(self.payment(order).transactions.filter(transaction_type="refund").count(), 1)
        self.assertEqual(self.consistent()["order_cancelled"], 1)

    def test_8_a_partial_cod_refund_changes_no_stock_and_the_rest_cancels_the_order(self):
        order = self.place_cod()
        self.capture(order)
        payment = self.payment(order)

        r = self.staff.post(f"/api/v1/payments/{payment.pk}/refund/", {"amount": "100.00"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((Order.objects.get(pk=order["id"]).status, self.stock()), ("open", (8, 2)))
        self.assertEqual(self.consistent()["order_cancelled"], 0)

        r = self.staff.post(f"/api/v1/payments/{payment.pk}/refund/", {"amount": "898.00"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((Order.objects.get(pk=order["id"]).status, self.stock()), ("cancelled", (10, 0)))
        self.assertEqual(self.consistent()["order_cancelled"], 1)


class AvailabilityAgreementTests(CommerceIntegrationBase):
    """Every number a shopper sees or is checked against is max(sum of available over all locations, 0)."""

    def make_stock(self, here, shop=None):
        """continue_selling on, `here` at the main warehouse (can be negative: oversold), `shop` at a second one."""
        from apps.inventory.services import adjust

        self.variant.continue_selling = True
        self.variant.save()
        set_available(self.variant, self.location, 0)
        if here:
            adjust(self.variant, self.location, here)
        if shop is not None:
            set_available(self.variant, Location.objects.get_or_create(store=self.store, name="Shop floor")[0], shop)

    def numbers(self):
        from apps.catalog.services import published_products
        from apps.themes.rendering import product_drop

        drop = product_drop(published_products(self.store).get(pk=self.product.pk), "/s/shop")
        api = self.staff.get(f"/api/v1/inventory/items/availability/?variant={self.variant.pk}").json()
        return (sellable(self.variant), drop["available_total"], drop["variants"][0]["available_total"], api["quantity"])

    def test_storefront_cart_checks_and_the_availability_api_give_the_same_number(self):
        self.make_stock(-2, shop=5)  # -2 here (oversold), 5 at the shop: 3 to sell
        self.assertEqual(self.numbers(), (3, 3, 3, 3))

    def test_a_negative_total_is_zero_for_shoppers_and_the_api(self):
        self.make_stock(-2)
        self.assertEqual(self.numbers(), (0, 0, 0, 0))

    def test_the_admin_keeps_the_plain_sum_so_staff_can_see_oversold_stock(self):
        self.make_stock(-2)
        product = self.staff.get(f"/api/v1/catalog/products/{self.product.pk}/").json()
        self.assertEqual((product["quantity"], product["variants"][0]["quantity"]), (-2, -2))
        self.make_stock(-2, shop=5)
        product = self.staff.get(f"/api/v1/catalog/products/{self.product.pk}/").json()
        self.assertEqual((product["quantity"], product["variants"][0]["quantity"]), (3, 3))

    def test_an_untracked_variant_is_in_stock_on_the_storefront_like_the_admin(self):
        from apps.catalog.models import Product, ProductSalesChannel, ProductVariant
        from apps.catalog.services import online_channels, published_products
        from apps.themes.rendering import product_drop

        product = Product.objects.create(store=self.store, title="Gift card", status="active")
        variant = ProductVariant.objects.create(product=product, sku="GIFT-1", price=Decimal("50.00"))
        for channel in online_channels(self.store):
            ProductSalesChannel.objects.create(product=product, sales_channel=channel)

        self.assertEqual(sellable(variant), 0)  # no stock number, but stock does not limit it
        drop = product_drop(published_products(self.store).get(pk=product.pk), "/s/shop")
        self.assertFalse(drop["is_sold_out"])
        self.assertEqual((drop["variants"][0]["tracked"], drop["variants"][0]["is_sold_out"]), (False, False))
        api = self.staff.get(f"/api/v1/inventory/items/availability/?variant={variant.pk}").json()
        self.assertEqual((api["tracked"], api["inStock"], api["quantity"]), (False, True, None))  # the admin: in stock

        # A tracked variant at 0 is still sold out, next to an untracked one
        tracked = ProductVariant.objects.create(product=product, sku="GIFT-2", price=Decimal("60.00"))
        set_available(tracked, default_location(self.store), 0)
        drop = product_drop(published_products(self.store).get(pk=product.pk), "/s/shop")
        by_sku = {v["sku"]: v for v in drop["variants"]}
        self.assertEqual((by_sku["GIFT-1"]["is_sold_out"], by_sku["GIFT-2"]["is_sold_out"]), (False, True))
        self.assertFalse(drop["is_sold_out"])

    def test_an_untracked_variant_can_be_added_to_the_cart_and_checked_out(self):
        from apps.catalog.models import Product, ProductSalesChannel, ProductVariant
        from apps.catalog.services import online_channels
        from apps.commerce import services as commerce
        from apps.inventory.services import sells_without_stock

        product = Product.objects.create(store=self.store, title="Gift card", status="active")
        variant = ProductVariant.objects.create(product=product, sku="GIFT-1", price=Decimal("50.00"))
        for channel in online_channels(self.store):
            ProductSalesChannel.objects.create(product=product, sales_channel=channel)
        self.assertTrue(sells_without_stock(variant))
        commerce._check_stock(product, variant, 5)  # no "sold out" and no "only N left"
        commerce._check_stock(product, variant, 5, checkout_id=uuid.uuid4())

        tracked = ProductVariant.objects.create(product=product, sku="GIFT-2", price=Decimal("60.00"))
        set_available(tracked, default_location(self.store), 0)
        self.assertFalse(sells_without_stock(tracked))
        with self.assertRaises(ValidationError):
            commerce._check_stock(product, tracked, 1)


class CartPanelTests(CommerceIntegrationBase):
    """The cart panel the storefront shows: a product that left the store is flagged, and stock is read in bulk."""

    def cart_of(self, session, products):
        cart = services.get_or_create_cart(self.store, session_id=session)
        for product in products:
            services.add_item(cart, product, 1)
        return cart

    def test_a_product_that_is_no_longer_for_sale_is_flagged_in_the_cart_panel(self):
        from apps.themes import cart as cart_data

        hat, _ = stock_product(self.store, "Hat", "HAT-1", "10.00", 5, self.location)
        cart = self.cart_of("panel-flag-aaaaaaaaaa", [self.product, hat])
        Product.objects.filter(pk=hat.pk).update(status="draft")

        lines = {line["title"]: line for line in cart_data.view_data(cart, "/s/shop")["lines"]}

        self.assertEqual((lines["Hat"]["warning"], lines["Hat"]["at_max"]), ("No longer available", True))
        self.assertEqual(lines["Tee"]["warning"], "")
        ProductSalesChannel.objects.filter(product=self.product).delete()  # off the Online Store counts the same
        lines = {line["title"]: line for line in cart_data.view_data(cart, "/s/shop")["lines"]}
        self.assertEqual(lines["Tee"]["warning"], "No longer available")

    def test_the_cart_panel_uses_the_same_stock_number_as_the_checks(self):
        from apps.themes import cart as cart_data

        set_available(self.variant, self.location, 3)
        cart = self.cart_of("panel-stock-aaaaaaaaaa", [self.product])
        set_available(self.variant, self.location, 0)
        self.assertEqual(cart_data.view_data(cart, "/s/shop")["lines"][0]["warning"], "Sold out")
        set_available(self.variant, self.location, 2)
        line_item = services.active_items(cart).get()
        line_item.quantity = 5
        line_item.save()
        self.assertEqual(cart_data.view_data(cart, "/s/shop")["lines"][0]["warning"], "Only 2 left")

    def test_the_cart_panel_reads_stock_in_a_fixed_number_of_queries(self):
        from django.test.utils import CaptureQueriesContext

        from apps.themes import cart as cart_data

        counts = []
        for lines in (1, 5):
            products = [self.product] + [
                stock_product(self.store, f"Extra {lines}-{n}", f"EX-{lines}-{n}", "10.00", 5, self.location)[0]
                for n in range(lines - 1)
            ]
            cart = self.cart_of(f"panel-count-{lines}-aaaaaaaa", products)
            with CaptureQueriesContext(connection) as queries:
                cart_data.view_data(cart, "/s/shop")
            counts.append(len(queries))
        self.assertEqual(counts[0], counts[1], counts)  # not one more query per line

    def test_the_cart_panel_shows_todays_prices_and_saves_them(self):
        from apps.themes import cart as cart_data

        hat, _ = stock_product(self.store, "Hat", "HAT-1", "10.00", 5, self.location)
        cart = self.cart_of("panel-price-aaaaaaaaaa", [self.product, hat])
        ProductVariant.objects.filter(pk=self.variant.pk).update(price=Decimal("450.50"))  # the owner changes a price

        data = cart_data.view_data(cart, "/s/shop")

        lines = {line["title"]: line for line in data["lines"]}
        self.assertEqual((lines["Tee"]["unit_price"], lines["Tee"]["total"]), ("450.50", "450.50"))
        self.assertEqual(lines["Hat"]["unit_price"], "10.00")
        self.assertEqual(data["subtotal"], "460.50")
        # saved, so the cart API and the checkout that starts next read the same price
        self.assertEqual(sorted(str(i.unit_price) for i in services.active_items(cart)), ["10.00", "450.50"])

    def test_changed_prices_cost_one_query_however_many_lines_changed(self):
        from django.test.utils import CaptureQueriesContext

        from apps.themes import cart as cart_data

        def queries(cart):
            with CaptureQueriesContext(connection) as captured:
                cart_data.view_data(cart, "/s/shop")
            return len(captured)

        counts = []
        for lines in (1, 5):
            products = [self.product] + [
                stock_product(self.store, f"Extra {lines}-{n}", f"EX-{lines}-{n}", "10.00", 5, self.location)[0]
                for n in range(lines - 1)
            ]
            cart = self.cart_of(f"panel-write-{lines}-aaaaaaaa", products)
            unchanged = queries(cart)
            for number, product in enumerate(products):
                ProductVariant.objects.filter(product=product).update(price=Decimal(f"{20 + lines}.{number}0"))
            changed = queries(cart)
            self.assertEqual(changed, unchanged + 1, lines)  # one write for all the lines that changed
            self.assertEqual(
                sorted(str(i.unit_price) for i in services.active_items(cart)),
                sorted(f"{20 + lines}.{n}0" for n in range(lines)),
            )
            counts.append(changed)
        self.assertEqual(counts[0], counts[1])

    def test_a_cart_panel_with_unchanged_prices_writes_nothing(self):
        from django.test.utils import CaptureQueriesContext

        from apps.themes import cart as cart_data

        cart = self.cart_of("panel-same-aaaaaaaaaaa", [self.product])
        before = services.active_items(cart).get().updated_at
        with CaptureQueriesContext(connection) as captured:
            cart_data.view_data(cart, "/s/shop")
        self.assertFalse([q for q in captured.captured_queries if q["sql"].lstrip().upper().startswith(("UPDATE", "INSERT"))])
        self.assertEqual(services.active_items(cart).get().updated_at, before)
