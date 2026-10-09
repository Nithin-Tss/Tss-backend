from decimal import Decimal

from django.test import override_settings
from django.urls import include, path
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase

from apps.catalog.models import Product, ProductVariant
from apps.tenancy.models import Store

from . import services


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
