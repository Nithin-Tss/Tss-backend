from datetime import timedelta
from decimal import Decimal

from django.test import override_settings
from django.urls import include, path
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase

from apps.catalog.models import Product, ProductSalesChannel, ProductVariant
from apps.catalog.services import online_channels
from apps.customers.models import Customer
from apps.discounts.models import Discount, DiscountCode, OrderDiscount
from apps.inventory.models import Location
from apps.inventory.services import set_available
from apps.orders.models import Order
from apps.tenancy.models import Store

from . import services
from .models import Cart


urlpatterns = [
    path("api/v1/", include("apps.commerce.urls")),
    path("api/v1/storefront/", include("apps.commerce.storefront_urls")),
]


@override_settings(ROOT_URLCONF=__name__)
class CommerceTestBase(APITestCase):
    def setUp(self):
        self.store = Store.objects.create(store_name="Shop", store_slug="shop", status="active")
        self.product = Product.objects.create(store=self.store, title="Tee", status="active")
        self.variant = ProductVariant.objects.create(product=self.product, sku="TEE-1", price=Decimal("499.00"))
        # Shoppers can only buy what is on the Online Store and in stock
        for channel in online_channels(self.store):
            ProductSalesChannel.objects.create(product=self.product, sales_channel=channel)
        self.location = Location.objects.create(store=self.store, name="Main warehouse")
        set_available(self.variant, self.location, 10)


class CartServiceTests(CommerceTestBase):
    def test_same_session_gets_same_cart(self):
        a = services.get_or_create_cart(self.store, session_id="s1")
        b = services.get_or_create_cart(self.store, session_id="s1")
        self.assertEqual(a.pk, b.pk)

    def test_adding_twice_raises_quantity(self):
        cart = services.get_or_create_cart(self.store, session_id="s1")
        services.add_item(cart, self.product, 1)
        services.add_item(cart, self.product, 2)
        items = list(services.active_items(cart))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].quantity, 3)
        self.assertEqual(services.cart_subtotal(cart), Decimal("1497.00"))

    def test_draft_product_rejected(self):
        self.product.status = "draft"
        self.product.save()
        cart = services.get_or_create_cart(self.store, session_id="s1")
        with self.assertRaises(ValidationError):
            services.add_item(cart, self.product, 1)

    def test_sold_out_is_blocked_with_a_message(self):
        cart = services.get_or_create_cart(self.store, session_id="s1")
        item = services.add_item(cart, self.product, 10)
        with self.assertRaises(ValidationError) as raised:
            services.set_quantity(cart, item.pk, 11)
        self.assertIn("Only 10 of Tee left", str(raised.exception))

        set_available(self.variant, self.location, 0)
        with self.assertRaises(ValidationError) as raised:
            services.add_item(cart, self.product, 1)
        self.assertIn("Tee is sold out", str(raised.exception))
        with self.assertRaises(ValidationError):
            services.start_checkout(cart)  # ran out after it was added

    def test_zero_quantity_removes_line(self):
        cart = services.get_or_create_cart(self.store, session_id="s1")
        item = services.add_item(cart, self.product, 1)
        services.set_quantity(cart, item.pk, 0)
        self.assertEqual(list(services.active_items(cart)), [])


class CheckoutServiceTests(CommerceTestBase):
    def test_checkout_freezes_cart_and_completes(self):
        cart = services.get_or_create_cart(self.store, session_id="s1")
        services.add_item(cart, self.product, 2)
        checkout = services.start_checkout(cart, email="a@b.com")
        self.assertEqual(checkout.total_amount, Decimal("998.00"))
        self.assertEqual(checkout.items.count(), 1)

        again = services.start_checkout(cart)
        self.assertEqual(again.pk, checkout.pk)
        self.assertEqual(again.items.count(), 1)

        services.complete_checkout(checkout)
        cart.refresh_from_db()
        self.assertEqual(cart.status, services.CART_CHECKED_OUT)

    def test_empty_cart_cannot_check_out(self):
        cart = services.get_or_create_cart(self.store, session_id="s1")
        with self.assertRaises(ValidationError):
            services.start_checkout(cart)


class StorefrontApiTests(CommerceTestBase):
    base = "/api/v1/storefront/shop"

    def test_cart_to_checkout_flow(self):
        h = {"HTTP_X_CART_SESSION": "sess-1"}
        r = self.client.post(f"{self.base}/cart/items/", {"product": str(self.product.pk), "quantity": 2}, format="json", **h)
        self.assertEqual(r.status_code, 201)
        item_id = r.data["items"][0]["id"]

        r = self.client.patch(f"{self.base}/cart/items/{item_id}/", {"quantity": 3}, format="json", **h)
        self.assertEqual(r.data["items"][0]["quantity"], 3)

        r = self.client.post(f"{self.base}/checkout/", {"email": "a@b.com"}, format="json", **h)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data["total_amount"], "1497.00")

        other = self.client.get(f"{self.base}/checkout/{r.data['id']}/", HTTP_X_CART_SESSION="someone-else")
        self.assertEqual(other.status_code, 404)

    def test_requires_session_header(self):
        self.assertEqual(self.client.get(f"{self.base}/cart/").status_code, 400)

    def test_unknown_store_404(self):
        self.assertEqual(self.client.get("/api/v1/storefront/nope/cart/", HTTP_X_CART_SESSION="s").status_code, 404)

    def test_staff_urls_require_auth(self):
        self.assertIn(self.client.get("/api/v1/carts/").status_code, (401, 403))


class PlaceOrderTests(CommerceTestBase):
    base = "/api/v1/storefront/shop"
    h = {"HTTP_X_CART_SESSION": "sess-1"}
    address = {"name": "A", "line1": "1 Main St", "city": "Chennai", "postal_code": "600001", "country": "IN"}

    def _checkout(self, quantity=2, **extra):
        self.client.post(f"{self.base}/cart/items/", {"product": str(self.product.pk), "quantity": quantity}, format="json", **self.h)
        r = self.client.post(f"{self.base}/checkout/", extra, format="json", **self.h)
        self.assertEqual(r.status_code, 201, r.data)
        return r.data["id"]

    def test_place_order_commits_stock_and_is_repeat_safe(self):
        cid = self._checkout(email="a@b.com", shipping_address=self.address)
        r = self.client.post(f"{self.base}/checkout/{cid}/complete/", **self.h)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["total"], "998.00")
        self.assertEqual(r.data["status"], "open")

        again = self.client.post(f"{self.base}/checkout/{cid}/complete/", **self.h)
        self.assertEqual(again.data["id"], r.data["id"])
        self.assertEqual(Order.objects.count(), 1)
        # 10 - 2 sold, once
        from apps.inventory.services import sellable
        self.assertEqual(sellable(self.variant), 8)

        got = self.client.get(f"{self.base}/orders/{r.data['id']}/", **self.h)
        self.assertEqual(got.status_code, 200)
        other = self.client.get(f"{self.base}/orders/{r.data['id']}/", HTTP_X_CART_SESSION="someone-else")
        self.assertEqual(other.status_code, 404)

    def test_needs_email_and_address(self):
        cid = self._checkout()
        r = self.client.post(f"{self.base}/checkout/{cid}/complete/", **self.h)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Order.objects.count(), 0)

    def test_discount_code_applies_and_is_recorded(self):
        discount = Discount.objects.create(
            store=self.store, name="Ten off", discount_type="percentage", value=Decimal("10"),
            starts_at=timezone.now() - timedelta(days=1), is_active=True, is_deleted=False,
        )
        DiscountCode.objects.create(discount=discount, code="SAVE10", is_active=True, is_deleted=False)
        cid = self._checkout(email="a@b.com", shipping_address=self.address, discount_code="save10")
        r = self.client.get(f"{self.base}/checkout/{cid}/", **self.h)
        self.assertEqual(r.data["discount_amount"], "99.80")
        self.assertEqual(r.data["total_amount"], "898.20")

        order = self.client.post(f"{self.base}/checkout/{cid}/complete/", **self.h).data
        self.assertEqual(order["total"], "898.20")
        self.assertEqual(OrderDiscount.objects.filter(order_id=order["id"]).count(), 1)

        bad = self.client.patch(f"{self.base}/checkout/{cid}/", {"discount_code": "nope"}, format="json", **self.h)
        self.assertEqual(bad.status_code, 400)

    def test_stock_gone_at_completion_blocks_order(self):
        cid = self._checkout(email="a@b.com", shipping_address=self.address)
        set_available(self.variant, self.location, 1)
        r = self.client.post(f"{self.base}/checkout/{cid}/complete/", **self.h)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Order.objects.count(), 0)

    def test_list_and_cancel_own_orders(self):
        from apps.inventory.services import sellable
        cid = self._checkout(email="a@b.com", shipping_address=self.address)
        order = self.client.post(f"{self.base}/checkout/{cid}/complete/", **self.h).data
        self.assertEqual(sellable(self.variant), 8)

        listed = self.client.get(f"{self.base}/orders/", **self.h)
        self.assertEqual([o["id"] for o in listed.data], [order["id"]])
        self.assertEqual(self.client.get(f"{self.base}/orders/", HTTP_X_CART_SESSION="other").data, [])

        # someone else's session can't cancel it
        theirs = self.client.post(f"{self.base}/orders/{order['id']}/cancel/", HTTP_X_CART_SESSION="other")
        self.assertEqual(theirs.status_code, 404)

        r = self.client.post(f"{self.base}/orders/{order['id']}/cancel/", **self.h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["status"], "cancelled")
        self.assertEqual(sellable(self.variant), 10)

        again = self.client.post(f"{self.base}/orders/{order['id']}/cancel/", **self.h)
        self.assertEqual(again.status_code, 400)
        self.assertEqual(sellable(self.variant), 10)


class CartMergeTests(CommerceTestBase):
    def setUp(self):
        super().setUp()
        self.customer = Customer.objects.create(store=self.store, first_name="A", email="a@b.com")
        self.other = Product.objects.create(store=self.store, title="Cap", status="active")
        self.other_variant = ProductVariant.objects.create(product=self.other, sku="CAP-1", price=Decimal("199.00"))
        for channel in online_channels(self.store):
            ProductSalesChannel.objects.create(product=self.other, sales_channel=channel)
        set_available(self.other_variant, self.location, 5)

    def test_guest_cart_merges_into_customer_cart(self):
        mine = services.get_or_create_cart(self.store, customer=self.customer)
        services.add_item(mine, self.product, 2)
        guest = services.get_or_create_cart(self.store, session_id="s1")
        services.add_item(guest, self.product, 3)
        services.add_item(guest, self.other, 1)

        merged = services.claim_session_cart(self.store, "s1", self.customer)
        self.assertEqual(merged.pk, mine.pk)
        lines = {i.product_id: i.quantity for i in services.active_items(merged)}
        self.assertEqual(lines, {self.product.pk: 5, self.other.pk: 1})
        guest.refresh_from_db()
        self.assertEqual(guest.status, services.CART_MERGED)
        self.assertEqual(list(services.active_items(guest)), [])

        # the session now starts a fresh guest cart
        self.assertNotEqual(services.get_or_create_cart(self.store, session_id="s1").pk, guest.pk)

    def test_merge_caps_at_stock_and_drops_sold_out(self):
        mine = services.get_or_create_cart(self.store, customer=self.customer)
        services.add_item(mine, self.product, 8)
        guest = services.get_or_create_cart(self.store, session_id="s1")
        services.add_item(guest, self.product, 5)
        services.add_item(guest, self.other, 1)
        set_available(self.other_variant, self.location, 0)

        merged = services.merge_carts(guest, mine)
        lines = {i.product_id: i.quantity for i in services.active_items(merged)}
        self.assertEqual(lines, {self.product.pk: 10})

    def test_no_guest_cart_is_a_no_op(self):
        cart = services.claim_session_cart(self.store, "nobody", self.customer)
        self.assertEqual(cart.customer_id, self.customer.pk)
        self.assertEqual(Cart.objects.count(), 1)
