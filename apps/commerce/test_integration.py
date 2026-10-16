"""
End-to-end commerce flows: the public storefront API and the staff API working
against the same store, with real sign-up, store membership and permissions.
"""
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.catalog.models import Product, ProductSalesChannel, ProductVariant
from apps.catalog.services import online_channels
from apps.discounts.models import Discount, DiscountCode
from apps.inventory.models import Location
from apps.inventory.services import sellable, set_available
from apps.orders.models import Order
from apps.tenancy.models import Store

from . import services
from .models import Cart, Checkout

ADDRESS = {"name": "A", "line1": "1 Main St", "city": "Chennai", "postal_code": "600001", "country": "IN"}


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
        self.staff, self.store = sign_up_with_store("owner@example.com")
        self.store.status = "active"
        self.store.save()
        self.base = f"/api/v1/storefront/{self.store.store_slug}"
        self.location = Location.objects.create(store=self.store, name="Main warehouse")
        self.product, self.variant = stock_product(self.store, "Tee", "TEE-1", "499.00", 10, self.location)

        self.shopper = APIClient()
        self.shopper.credentials(HTTP_X_CART_SESSION="sess-1")

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
        self.assertEqual(self.staff.get("/api/v1/carts/?status=open").json()[0]["session_id"], "sess-1")

        order = self.place(cid)
        self.assertEqual(order.status_code, 201, order.data)

        # The cart and checkout close, the order shows up in the back office
        self.assertEqual(self.staff.get("/api/v1/carts/?status=open").json(), [])
        self.assertEqual(self.staff.get("/api/v1/carts/?status=checked_out").json()[0]["session_id"], "sess-1")
        self.assertEqual(self.staff.get("/api/v1/checkouts/?status=completed").json()[0]["id"], cid)
        self.assertEqual(Order.objects.get().total, Decimal("998.00"))
        self.assertEqual(sellable(self.variant), 8)

        # A returning session starts from an empty cart
        self.assertEqual(self.shopper.get(f"{self.base}/cart/").data["items"], [])

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
        second.credentials(HTTP_X_CART_SESSION="sess-2")

        first_cid = self.checkout(quantity=1, email="a@b.com", shipping_address=ADDRESS)
        second_cid = self.checkout(quantity=1, client=second, email="c@d.com", shipping_address=ADDRESS)

        self.assertEqual(self.place(first_cid).status_code, 201)
        self.assertEqual(self.place(second_cid, client=second).status_code, 400)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(sellable(self.variant), 0)

    def test_price_change_after_checkout_does_not_affect_the_order(self):
        cid = self.checkout(quantity=1, email="a@b.com", shipping_address=ADDRESS)
        self.variant.price = Decimal("999.00")
        self.variant.save()
        order = self.place(cid)
        self.assertEqual(order.data["total"], "499.00")


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
