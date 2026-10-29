from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core.cache import cache
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
from apps.tenancy.models import ShippingRate, Store, TaxRate

from . import services
from .models import Cart


urlpatterns = [
    path("api/v1/", include("apps.commerce.urls")),
    path("api/v1/storefront/", include("apps.commerce.storefront_urls")),
]


def assert_stock_consistent(case, store):
    """
    The stock rules, for every stock row of the store, to call after each step of a journey:
    on_hand = available + committed + unavailable; committed and unavailable never negative; available
    never negative unless the variant sells past its stock (continue_selling); and every change left a
    history row: what the history adds up to is exactly what the row holds, and each history row is itself
    consistent. Returns {reason: how many history rows}, so a test can check which changes were recorded.
    """
    from collections import Counter

    from django.db.models import Sum

    from apps.inventory.models import InventoryHistory, InventoryItem

    for item in InventoryItem.objects.filter(variant__product__store=store).select_related("variant", "location"):
        where = f"{item.variant.sku} at {item.location.name}"
        case.assertEqual(item.on_hand, item.available + item.committed + item.unavailable, f"on_hand identity, {where}")
        case.assertGreaterEqual(item.committed, 0, f"committed, {where}")
        case.assertGreaterEqual(item.unavailable, 0, f"unavailable, {where}")
        if not item.variant.continue_selling:
            case.assertGreaterEqual(item.available, 0, f"available, {where}")
            case.assertGreaterEqual(item.on_hand, 0, f"on_hand, {where}")
        rows = InventoryHistory.objects.filter(store=store, variant=item.variant, location=item.location)
        totals = rows.aggregate(a=Sum("change"), c=Sum("committed_change"), u=Sum("unavailable_change"))
        case.assertEqual(
            (totals["a"] or 0, totals["c"] or 0, totals["u"] or 0),
            (item.available, item.committed, item.unavailable),
            f"history adds up to the stock row, {where}",
        )
        for row in rows:
            case.assertEqual(row.on_hand, row.after + row.committed + row.unavailable, f"history row {row.reason}, {where}")
    return Counter(InventoryHistory.objects.filter(store=store).values_list("reason", flat=True))


@override_settings(ROOT_URLCONF=__name__)
class CommerceTestBase(APITestCase):
    def setUp(self):
        cache.clear()  # storefront calls are rate limited; every test starts with a fresh allowance
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
        h = {"HTTP_X_CART_SESSION": "sess-1-aaaaaaaaaaaa"}
        r = self.client.post(f"{self.base}/cart/items/", {"product": str(self.product.pk), "quantity": 2}, format="json", **h)
        self.assertEqual(r.status_code, 201)
        item_id = r.data["items"][0]["id"]

        r = self.client.patch(f"{self.base}/cart/items/{item_id}/", {"quantity": 3}, format="json", **h)
        self.assertEqual(r.data["items"][0]["quantity"], 3)

        r = self.client.post(f"{self.base}/checkout/", {"email": "a@b.com"}, format="json", **h)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data["total_amount"], "1497.00")

        other = self.client.get(f"{self.base}/checkout/{r.data['id']}/", HTTP_X_CART_SESSION="someone-else-aaaaaaaa")
        self.assertEqual(other.status_code, 404)

    def test_checkout_start_rejects_stock_reduced_after_add_to_cart(self):
        h = {"HTTP_X_CART_SESSION": "sess-1-aaaaaaaaaaaa"}
        added = self.client.post(
            f"{self.base}/cart/items/", {"product": str(self.product.pk), "quantity": 2}, format="json", **h
        )
        self.assertEqual(added.status_code, 201, added.data)
        set_available(self.variant, self.location, 1)

        checkout = self.client.post(f"{self.base}/checkout/", {"email": "a@b.com"}, format="json", **h)

        self.assertEqual(checkout.status_code, 400)
        self.assertIn("Only 1 of Tee left", str(checkout.data))

    def test_untracked_variant_is_sold_out_at_checkout_unless_continue_selling(self):
        from apps.inventory.models import InventoryItem

        h = {"HTTP_X_CART_SESSION": "sess-1-aaaaaaaaaaaa"}
        added = self.client.post(
            f"{self.base}/cart/items/", {"product": str(self.product.pk), "quantity": 1}, format="json", **h
        )
        self.assertEqual(added.status_code, 201, added.data)
        InventoryItem.objects.filter(variant=self.variant).delete()

        sold_out = self.client.post(f"{self.base}/checkout/", {}, format="json", **h)
        self.assertEqual(sold_out.status_code, 400)
        self.assertIn("Tee is sold out", str(sold_out.data))

        self.variant.continue_selling = True
        self.variant.save(update_fields=["continue_selling"])
        allowed = self.client.post(f"{self.base}/checkout/", {}, format="json", **h)
        self.assertEqual(allowed.status_code, 201, allowed.data)

    def test_requires_session_header(self):
        self.assertEqual(self.client.get(f"{self.base}/cart/").status_code, 400)

    def test_unknown_store_404(self):
        self.assertEqual(self.client.get("/api/v1/storefront/nope/cart/", HTTP_X_CART_SESSION="s-aaaaaaaaaaaaaaaa").status_code, 404)

    def test_staff_urls_require_auth(self):
        self.assertIn(self.client.get("/api/v1/carts/").status_code, (401, 403))


class PlaceOrderTests(CommerceTestBase):
    base = "/api/v1/storefront/shop"
    h = {"HTTP_X_CART_SESSION": "sess-1-aaaaaaaaaaaa"}
    address = {"first_name": "A", "last_name": "B", "address_line_1": "1 Main St", "city": "Chennai", "state": "TN", "postal_code": "600001", "country": "India"}

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
        other = self.client.get(f"{self.base}/orders/{r.data['id']}/", HTTP_X_CART_SESSION="someone-else-aaaaaaaa")
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
        self.assertEqual(self.client.get(f"{self.base}/orders/", HTTP_X_CART_SESSION="other-aaaaaaaaaaaaaaa").data, [])

        # someone else's session can't cancel it
        theirs = self.client.post(f"{self.base}/orders/{order['id']}/cancel/", HTTP_X_CART_SESSION="other-aaaaaaaaaaaaaaa")
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


class CartHardeningTests(CommerceTestBase):
    def _checkout(self, session="s1"):
        cart = services.get_or_create_cart(self.store, session_id=session)
        services.add_item(cart, self.product, 2)
        return services.start_checkout(cart, email="a@b.com", shipping_address={"city": "X"})

    def test_price_change_is_picked_up(self):
        cart = services.get_or_create_cart(self.store, session_id="s1")
        services.add_item(cart, self.product, 1)
        self.variant.price = Decimal("599.00")
        self.variant.save()
        self.assertEqual(len(services.refresh_prices(cart)), 1)
        self.assertEqual(services.cart_subtotal(cart), Decimal("599.00"))

    def test_checkout_adds_tax_and_flat_shipping(self):
        self.store.tax_rate, self.store.shipping_flat_rate = Decimal("18.00"), Decimal("50.00")
        self.store.save()
        checkout = self._checkout()
        self.assertEqual(checkout.subtotal, Decimal("998.00"))
        self.assertEqual(checkout.tax_amount, Decimal("179.64"))
        self.assertEqual(checkout.shipping_amount, Decimal("50.00"))
        self.assertEqual(checkout.total_amount, Decimal("1227.64"))

    def test_free_shipping_threshold_and_tax_after_discount(self):
        self.store.shipping_flat_rate, self.store.free_shipping_threshold = Decimal("50.00"), Decimal("900.00")
        self.store.save()
        self.assertEqual(self._checkout().shipping_amount, Decimal("0.00"))

    def test_placing_order_records_pending_cod_payment_and_guest_customer(self):
        order = services.place_order(self._checkout())
        payment = order.payments.get()
        self.assertEqual((payment.status, payment.provider, payment.amount), ("pending", "cod", order.total))
        self.assertEqual(order.customer.email, "a@b.com")
        # repeat-safe: no second payment or customer
        services.place_order(services.Checkout.objects.get(pk=payment.checkout_id))
        self.assertEqual(order.payments.count(), 1)
        self.assertEqual(Customer.objects.filter(store=self.store).count(), 1)

    def test_unknown_payment_method_rejected(self):
        with self.assertRaises(ValidationError):
            services.place_order(self._checkout(), payment_method="bitcoin")

    def test_expire_skips_carts_in_checkout(self):
        idle = services.get_or_create_cart(self.store, session_id="idle")
        self._checkout("busy")
        Cart.objects.update(updated_at=timezone.now() - timedelta(days=60))
        self.assertEqual(services.expire_carts(self.store, timedelta(days=30)), 1)
        idle.refresh_from_db()
        self.assertEqual(idle.status, "expired")

    def test_sweep_command(self):
        from django.core.management import call_command
        self._checkout()
        services.Checkout.objects.update(updated_at=timezone.now() - timedelta(hours=3))
        call_command("sweep_carts", stdout=StringIO())
        self.assertTrue(services.Checkout.objects.get().abandoned_at)


class OrderCreationGapTests(CommerceTestBase):
    """Regression tests for the order-creation hardening."""

    base = PlaceOrderTests.base
    h = PlaceOrderTests.h
    address = PlaceOrderTests.address
    _checkout = PlaceOrderTests._checkout

    def _percent_code(self, **discount_fields):
        discount = Discount.objects.create(
            store=self.store, name="Ten off", discount_type="percentage", value=Decimal("10"),
            starts_at=timezone.now() - timedelta(days=1), is_active=True, is_deleted=False, **discount_fields,
        )
        return DiscountCode.objects.create(discount=discount, code="SAVE10", is_active=True, is_deleted=False)

    def _place(self, **checkout):
        cid = self._checkout(email="a@b.com", shipping_address=self.address, **checkout)
        return cid, self.client.post(f"{self.base}/checkout/{cid}/complete/", **self.h)

    # --- price drift
    def test_discount_change_after_checkout_needs_reconfirmation(self):
        code = self._percent_code()
        cid = self._checkout(email="a@b.com", shipping_address=self.address, discount_code="save10")
        code.discount.value = Decimal("50")
        code.discount.save()
        refused = self.client.post(f"{self.base}/checkout/{cid}/complete/", **self.h)
        self.assertEqual(refused.status_code, 400)
        shown = self.client.get(f"{self.base}/checkout/{cid}/", **self.h).data
        self.assertEqual(shown["discount_amount"], "499.00")
        self.assertEqual(self.client.post(f"{self.base}/checkout/{cid}/complete/", **self.h).status_code, 201)

    def test_unchanged_checkout_places_without_a_second_confirmation(self):
        self.assertEqual(self._place()[1].status_code, 201)

    # --- availability
    def test_product_unpublished_after_checkout_blocks_order(self):
        cid = self._checkout(email="a@b.com", shipping_address=self.address)
        self.product.status = "draft"
        self.product.save()
        self.assertEqual(self.client.post(f"{self.base}/checkout/{cid}/complete/", **self.h).status_code, 400)
        self.assertEqual(Order.objects.count(), 0)

    def test_product_without_variant_cannot_be_added(self):
        bare = Product.objects.create(store=self.store, title="Bare", status="active")
        for channel in online_channels(self.store):
            ProductSalesChannel.objects.create(product=bare, sales_channel=channel)
        cart = services.get_or_create_cart(self.store, session_id="s1")
        with self.assertRaises(ValidationError):
            services.add_item(cart, bare, 1)

    # --- cancelling
    def test_cancel_voids_pending_payment_and_frees_discount_use(self):
        from apps.discounts.services import times_used
        from apps.payments.models import Payment

        code = self._percent_code(usage_limit=1)
        cid, placed = self._place(discount_code="save10")
        self.assertEqual(times_used(code.discount), 1)

        self.client.post(f"{self.base}/orders/{placed.data['id']}/cancel/", **self.h)
        self.assertEqual(Payment.objects.get(order_id=placed.data["id"]).status, "cancelled")
        self.assertEqual(times_used(code.discount), 0)

    # --- discount limits
    def test_per_customer_limit_is_enforced(self):
        self._percent_code(per_customer_limit=1)
        self.assertEqual(self._place(discount_code="save10")[1].status_code, 201)
        # same email, new session, tries the code again
        self.h = {"HTTP_X_CART_SESSION": "sess-2-aaaaaaaaaaaa"}
        cid = self._checkout(email="a@b.com", shipping_address=self.address, discount_code="save10")
        r = self.client.post(f"{self.base}/checkout/{cid}/complete/", **self.h)
        self.assertEqual(r.status_code, 400)

    def test_usage_limit_cannot_be_passed_by_a_second_checkout(self):
        self._percent_code(usage_limit=1)
        cid_a = self._checkout(email="a@b.com", shipping_address=self.address, discount_code="save10")
        self.h = {"HTTP_X_CART_SESSION": "sess-2-aaaaaaaaaaaa"}
        cid_b = self._checkout(email="c@d.com", shipping_address=self.address, discount_code="save10")
        self.assertEqual(self.client.post(f"{self.base}/checkout/{cid_b}/complete/", **self.h).status_code, 201)
        self.h = {"HTTP_X_CART_SESSION": "sess-1-aaaaaaaaaaaa"}
        self.assertEqual(self.client.post(f"{self.base}/checkout/{cid_a}/complete/", **self.h).status_code, 400)

    # --- abuse
    def test_open_cod_orders_are_capped_per_customer(self):
        set_available(self.variant, self.location, 100)
        for n in range(services.MAX_OPEN_COD_ORDERS):
            self.h = {"HTTP_X_CART_SESSION": f"cap-session-{n}-aaaaaaaa"}
            self.assertEqual(self._place()[1].status_code, 201)
        self.h = {"HTTP_X_CART_SESSION": "cap-session-last-aaaaaa"}
        self.assertEqual(self._place()[1].status_code, 400)

    def test_address_must_be_complete(self):
        self.client.post(f"{self.base}/cart/items/", {"product": str(self.product.pk), "quantity": 1}, format="json", **self.h)
        r = self.client.post(f"{self.base}/checkout/", {"email": "a@b.com", "shipping_address": {"x": 1}}, format="json", **self.h)
        self.assertEqual(r.status_code, 400)
        self.assertIn("shipping_address", r.data)

    def test_weak_session_ids_are_refused(self):
        for bad in ("1", "short", "has spaces in it aaaaaaaa", "x" * 100):
            r = self.client.get(f"{self.base}/cart/", HTTP_X_CART_SESSION=bad)
            self.assertEqual(r.status_code, 400, bad)

    def test_order_placement_is_rate_limited(self):
        statuses = [
            self.client.post(f"{self.base}/checkout/", {}, format="json", **self.h).status_code for _ in range(25)
        ]
        self.assertIn(429, statuses)


class ShippingCalculationTests(CommerceTestBase):
    """Flat fee, free-shipping threshold, and weight/country rates."""

    def _checkout(self, qty=2, country=None, discount_code=None):
        cart = services.get_or_create_cart(self.store, session_id="ship")
        services.add_item(cart, self.product, qty)
        address = {"city": "X"} if country is None else {"city": "X", "country": country}
        return services.start_checkout(cart, email="a@b.com", shipping_address=address, discount_code=discount_code)

    def _rate(self, price, country="", min_weight="0", max_weight=None):
        return ShippingRate.objects.create(
            store=self.store, country=country, price=Decimal(price),
            min_weight=Decimal(min_weight), max_weight=None if max_weight is None else Decimal(max_weight),
        )

    def test_flat_rate_when_no_rates_defined(self):
        self.store.shipping_flat_rate = Decimal("40.00")
        self.store.save()
        self.assertEqual(self._checkout().shipping_amount, Decimal("40.00"))

    def test_threshold_met_waives_shipping(self):
        self.store.shipping_flat_rate, self.store.free_shipping_threshold = Decimal("40.00"), Decimal("998.00")
        self.store.save()
        self.assertEqual(self._checkout().shipping_amount, Decimal("0.00"))

    def test_threshold_checked_after_discount(self):
        self.store.shipping_flat_rate, self.store.free_shipping_threshold = Decimal("40.00"), Decimal("998.00")
        self.store.save()
        discount = Discount.objects.create(
            store=self.store, name="Ten off", discount_type="fixed_amount", value=Decimal("10.00"),
            starts_at=timezone.now() - timedelta(days=1), is_active=True, is_deleted=False,
        )
        DiscountCode.objects.create(discount=discount, code="TEN", is_active=True, is_deleted=False)
        checkout = self._checkout(discount_code="TEN")
        self.assertEqual(checkout.discount_amount, Decimal("10.00"))
        self.assertEqual(checkout.shipping_amount, Decimal("40.00"))

    def test_weight_band_picks_matching_rate(self):
        self.variant.weight = Decimal("1.500")
        self.variant.save()
        self._rate("30.00", min_weight="0", max_weight="3")
        self._rate("80.00", min_weight="3")
        self.assertEqual(self._checkout(qty=1).shipping_amount, Decimal("30.00"))  # 1.5 kg
        self.assertEqual(self._checkout(qty=2).shipping_amount, Decimal("80.00"))  # 3 kg: upper bound is exclusive

    def test_country_rate_beats_anywhere_rate(self):
        self._rate("100.00")
        self._rate("60.00", country="India")
        self.assertEqual(self._checkout(country="india").shipping_amount, Decimal("60.00"))
        self.assertEqual(self._checkout(country="France").shipping_amount, Decimal("100.00"))
        self.assertEqual(self._checkout().shipping_amount, Decimal("100.00"))  # no country given

    def test_falls_back_to_flat_rate_when_no_rate_fits(self):
        self.store.shipping_flat_rate = Decimal("25.00")
        self.store.save()
        self._rate("60.00", country="India")
        self.assertEqual(self._checkout(country="France").shipping_amount, Decimal("25.00"))

    def test_free_shipping_overrides_rates(self):
        self.store.free_shipping_threshold = Decimal("500.00")
        self.store.save()
        self._rate("60.00")
        self.assertEqual(self._checkout().shipping_amount, Decimal("0.00"))

    def test_tax_ignores_shipping_and_total_adds_it(self):
        self.store.tax_rate = Decimal("10.00")
        self.store.shipping_flat_rate = Decimal("50.00")
        self.store.save()
        checkout = self._checkout()
        self.assertEqual(checkout.tax_amount, Decimal("99.80"))
        self.assertEqual(checkout.total_amount, Decimal("1147.80"))


class TaxRulesTests(CommerceTestBase):
    """Destination rates, exempt goods and customers, tax-inclusive prices, per-line tax."""

    def setUp(self):
        super().setUp()
        self.store.tax_rate = Decimal("10.00")
        self.store.save()

    def _checkout(self, qty=2, address=None, customer=None):
        # a fresh session each call: the cart is reused per session and quantities would add up
        self._sessions = getattr(self, "_sessions", 0) + 1
        cart = services.get_or_create_cart(self.store, session_id=f"tax-{self._sessions}", customer=customer)
        services.add_item(cart, self.product, qty)
        address = {"city": "X"} if address is None else {"city": "X", **address}
        return services.start_checkout(cart, email="a@b.com", shipping_address=address)

    def test_most_specific_rate_wins(self):
        TaxRate.objects.create(store=self.store, country="India", rate=Decimal("18.00"))
        TaxRate.objects.create(store=self.store, country="India", region="Kerala", rate=Decimal("12.00"))
        self.assertEqual(self._checkout(address={"country": "india", "state": "kerala"}).tax_amount, Decimal("119.76"))
        self.assertEqual(self._checkout(address={"country": "India", "state": "Goa"}).tax_amount, Decimal("179.64"))
        self.assertEqual(self._checkout(address={"country": "France"}).tax_amount, Decimal("99.80"))  # store rate

    def test_exempt_product_is_not_taxed(self):
        other = Product.objects.create(store=self.store, title="Book", status="active", is_taxable=False)
        v2 = ProductVariant.objects.create(product=other, sku="BK-1", price=Decimal("100.00"))
        for channel in online_channels(self.store):
            ProductSalesChannel.objects.create(product=other, sales_channel=channel)
        set_available(v2, self.location, 5)
        cart = services.get_or_create_cart(self.store, session_id="tax2")
        services.add_item(cart, self.product, 1)
        services.add_item(cart, other, 1)
        checkout = services.start_checkout(cart, email="a@b.com", shipping_address={"city": "X"})
        self.assertEqual(checkout.tax_amount, Decimal("49.90"))  # only the tee
        order = services._place_order(checkout, "cod")
        self.assertEqual({i.product_name: i.tax_amount for i in order.items.all()},
                         {"Tee": Decimal("49.90"), "Book": Decimal("0.00")})
        self.assertEqual(order.tax_amount, Decimal("49.90"))

    def test_tax_exempt_customer_pays_no_tax(self):
        customer = Customer.objects.create(store=self.store, email="r@b.com", tax_exempt=True)
        self.assertEqual(self._checkout(customer=customer).tax_amount, Decimal("0.00"))

    def test_inclusive_prices_report_tax_without_adding_it(self):
        self.store.prices_include_tax = True
        self.store.save()
        checkout = self._checkout()
        self.assertEqual(checkout.tax_amount, Decimal("90.73"))  # 998 * 10/110
        self.assertEqual(checkout.total_amount, Decimal("998.00"))
        order = services._place_order(checkout, "cod")
        self.assertEqual(order.total, Decimal("998.00"))
        self.assertEqual(order.tax_amount, Decimal("90.73"))

    def test_shipping_taxed_only_when_enabled(self):
        self.store.shipping_flat_rate = Decimal("50.00")
        self.store.save()
        self.assertEqual(self._checkout().tax_amount, Decimal("99.80"))
        self.store.tax_shipping = True
        self.store.save()
        self.assertEqual(self._checkout().tax_amount, Decimal("104.80"))

    def test_line_taxes_add_up_to_order_tax(self):
        from . import tax
        self.store.tax_rate = Decimal("7.50")
        result = tax.calculate(self.store, [{"amount": Decimal("0.33"), "taxable": True}] * 3,
                               discount=Decimal("0.10"))
        self.assertEqual(sum(result.line_taxes), result.total)
        self.assertEqual(result.total, Decimal("0.07"))  # (0.99 - 0.10) * 7.5%

    def test_manual_order_computes_tax_unless_given(self):
        from apps.orders import services as order_services
        line = [{"product": self.product, "variant": self.variant, "quantity": 1}]
        auto = order_services.create_order(self.store, line)
        self.assertEqual(auto.tax_amount, Decimal("49.90"))
        self.assertEqual(auto.total, Decimal("548.90"))
        given = order_services.create_order(self.store, line, tax_amount=Decimal("5.00"))
        self.assertEqual(given.tax_amount, Decimal("5.00"))
        self.assertEqual(given.items.get().tax_amount, Decimal("5.00"))
