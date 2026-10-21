"""Django Admin: store-owned records keep their store and never link to another store's records."""
import datetime
import json

from django.contrib import admin
from django.test import TestCase
from django.urls import reverse

from apps.catalog.models import Product, ProductVariant
from apps.customers.models import Customer
from apps.discounts.models import Discount, DiscountCode
from apps.identity.models import User
from apps.inventory.models import InventoryItem, Location, Transfer
from apps.orders.models import Order, OrderItem
from apps.payments.models import Payment
from apps.tenancy.models import Store, StoreMembership
from apps.wishlist.models import Wishlist

PHASE2_APPS = {"commerce", "customers", "delivery", "discounts", "fulfillment", "gift_cards", "inventory",
               "orders", "payments", "purchasing", "tenancy", "themes", "wishlist"}


class AdminStoreGuardTests(TestCase):
    def setUp(self):
        self.root = User.objects.create_superuser(email="root@example.com", password="Root-pass-123",
                                                  first_name="R", last_name="T")
        self.client.force_login(self.root)
        self.a, self.b = (self.make_store("Alpha Shop", "alpha"), self.make_store("Beta Shop", "beta"))

    def make_store(self, name, slug):
        store = Store.objects.create(store_name=name, store_slug=slug, status="active")
        store.product = Product.objects.create(store=store, title=f"{name} hat", status="active")
        store.variant = ProductVariant.objects.create(product=store.product, price="5")
        store.customer = Customer.objects.create(store=store, email_subscription_status="subscribed")
        store.location = Location.objects.create(store=store, name=f"{name} depot")
        store.order = Order.objects.create(
            store=store, order_number=f"{slug}-1", currency_code="INR", subtotal=0, discount_amount=0,
            tax_amount=0, shipping_amount=0, total=0, status="open", fulfillment_status="unfulfilled")
        return store

    def url(self, model, action, obj=None):
        name = f"admin:{model._meta.app_label}_{model._meta.model_name}_{action}"
        return reverse(name, args=[obj.pk] if obj else None)

    def form_data(self, obj, **changes):
        """The POST body of obj's change form as it stands, with changes applied."""
        form = self.client.get(self.url(type(obj), "change", obj)).context["adminform"].form
        data = {}
        for name in form.fields:
            value = form[name].value()
            if value is None or value is False:
                continue
            if value is True:
                value = "on"
            elif isinstance(value, (dict, list)):
                value = json.dumps(value)
            elif isinstance(value, datetime.datetime):
                data[name + "_0"], data[name + "_1"] = value.date().isoformat(), value.time().isoformat()
                continue
            data[name] = value
        data.update({k: getattr(v, "pk", v) for k, v in changes.items()})
        return data

    def post(self, model, data, obj=None):
        return self.client.post(self.url(model, "change" if obj else "add", obj), data)

    def errors(self, response):
        self.assertEqual(response.status_code, 200)  # the form came back instead of saving
        form = response.context["adminform"].form
        return {**form.errors, "__all__": form.non_field_errors()}

    def test_every_store_admin_page_loads_with_store_column_and_filter(self):
        for model, model_admin in admin.site._registry.items():
            if model._meta.app_label not in PHASE2_APPS:
                continue
            with self.subTest(model=model._meta.label):
                r = self.client.get(self.url(model, "changelist"))
                self.assertEqual(r.status_code, 200)
                if hasattr(model_admin, "store_path"):
                    self.assertIn("store_name", r.context["cl"].list_display)
                    self.assertIn(model_admin.store_path, r.context["cl"].list_filter)
                if model_admin.has_add_permission(r.wsgi_request):
                    self.assertEqual(self.client.get(self.url(model, "add")).status_code, 200)

    def test_lists_show_store_names_and_filter_by_store(self):
        OrderItem.objects.create(order=self.b.order, product=self.b.product, product_name="Hat", quantity=1,
                                 unit_price=5, discount_amount=0, tax_amount=0, total=5, is_deleted=False)
        r = self.client.get(self.url(Order, "changelist"))
        self.assertContains(r, "Alpha Shop")
        self.assertContains(r, "Beta Shop")
        r = self.client.get(self.url(Order, "changelist") + f"?store__store_id__exact={self.a.pk}")
        self.assertEqual([o.pk for o in r.context["cl"].result_list], [self.a.order.pk])
        r = self.client.get(self.url(OrderItem, "changelist") + f"?order__store__store_id__exact={self.b.pk}")
        self.assertEqual(r.context["cl"].result_count, 1)
        self.assertContains(r, "Beta Shop")

    def test_store_owner_cannot_enter_admin(self):
        owner = User.objects.create_user(email="owner@example.com", password="Owner-pass-123",
                                         first_name="O", last_name="W")
        StoreMembership.objects.create(user=owner, store=self.a, role="owner", status="active")
        self.client.force_login(owner)
        self.assertEqual(self.client.get(self.url(Order, "changelist")).status_code, 302)
        self.assertEqual(self.client.post(self.url(Location, "add"), {"store": self.a.pk, "name": "x"}).status_code, 302)
        self.assertFalse(Location.objects.filter(name="x").exists())

    def test_store_cannot_be_reassigned(self):
        membership = StoreMembership.objects.create(user=self.root, store=self.a, role="owner", status="active")
        for obj in (self.a.order, self.a.customer, self.a.location, membership):
            with self.subTest(model=type(obj).__name__):
                r = self.client.get(self.url(type(obj), "change", obj))
                self.assertNotContains(r, 'name="store"')
                r = self.post(type(obj), self.form_data(obj, store=self.b), obj)  # crafted POST
                self.assertEqual(r.status_code, 302)
                obj.refresh_from_db()
                self.assertEqual(obj.store_id, self.a.pk)

    def test_child_rows_cannot_move_to_another_stores_parent(self):
        item = OrderItem.objects.create(order=self.a.order, product=self.a.product, product_name="Hat", quantity=1,
                                        unit_price=5, discount_amount=0, tax_amount=0, total=5, is_deleted=False)
        discount = Discount.objects.create(store=self.a, name="Ten", discount_type="percentage", value=10,
                                           starts_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
                                           is_active=True, is_deleted=False)
        discount_b = Discount.objects.create(store=self.b, name="Ten", discount_type="percentage", value=10,
                                             starts_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
                                             is_active=True, is_deleted=False)
        code = DiscountCode.objects.create(discount=discount, code="TEN", is_active=True, is_deleted=False)
        for obj, field, other in ((item, "order", self.b.order), (code, "discount", discount_b)):
            with self.subTest(model=type(obj).__name__):
                r = self.post(type(obj), self.form_data(obj, **{field: other}), obj)
                self.assertEqual(r.status_code, 302)
                obj.refresh_from_db()
                self.assertNotEqual(getattr(obj, field + "_id"), other.pk)

    def test_order_item_rejects_another_stores_product_or_variant(self):
        item = OrderItem.objects.create(order=self.a.order, product=self.a.product, product_name="Hat", quantity=1,
                                        unit_price=5, discount_amount=0, tax_amount=0, total=5, is_deleted=False)
        self.assertIn("product", self.errors(self.post(OrderItem, self.form_data(item, product=self.b.product), item)))
        self.assertIn("variant", self.errors(self.post(OrderItem, self.form_data(item, variant=self.b.variant), item)))
        r = self.post(OrderItem, self.form_data(item, order=self.a.order, product=self.b.product))  # add page
        self.assertIn("product", self.errors(r))
        self.assertEqual(OrderItem.objects.count(), 1)
        r = self.post(OrderItem, self.form_data(item, order=self.a.order, variant=self.a.variant))
        self.assertEqual(r.status_code, 302)

    def test_store_records_reject_another_stores_links(self):
        transfer = Transfer.objects.create(store=self.a, transfer_number="T1", source_location=self.a.location,
                                           destination_location=Location.objects.create(store=self.a, name="Shop"),
                                           status="draft")
        payment = Payment.objects.create(store=self.a, currency="INR", amount=5, status="pending",
                                         provider="manual", is_deleted=False)
        wishlist = Wishlist.objects.create(store=self.a, customer=self.a.customer, name="Mine")
        cases = (
            (transfer, "destination_location", self.b.location),
            (payment, "order", self.b.order),
            (payment, "customer", self.b.customer),
            (wishlist, "customer", self.b.customer),
            (self.a.order, "customer", self.b.customer),
        )
        for obj, field, other in cases:
            with self.subTest(model=type(obj).__name__, field=field):
                self.assertIn(field, self.errors(self.post(type(obj), self.form_data(obj, **{field: other}), obj)))
                # also when creating a new record for store A
                r = self.post(type(obj), self.form_data(obj, store=self.a, **{field: other}))
                self.assertIn(field, self.errors(r))

    def test_inventory_item_location_and_variant_must_match(self):
        stock = InventoryItem.objects.create(variant=self.a.variant, location=self.a.location, unavailable=0,
                                             committed=0, available=1, on_hand=1, incoming=0)
        r = self.post(InventoryItem, self.form_data(stock, variant=self.b.variant, location=self.a.location))
        self.assertIn("variant", self.errors(r))
        # an existing row keeps its variant and location
        r = self.post(InventoryItem, self.form_data(stock, location=self.b.location, available=3), stock)
        self.assertEqual(r.status_code, 302)
        stock.refresh_from_db()
        self.assertEqual((stock.location_id, stock.available), (self.a.location.pk, 3))
