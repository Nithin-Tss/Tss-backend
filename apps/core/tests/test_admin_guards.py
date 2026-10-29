"""Django Admin: store-owned records keep their store and never link to another store's records."""
import datetime
import json

from django.contrib import admin
from django.test import TestCase
from django.urls import reverse

from apps.catalog.models import Product, ProductVariant
from apps.customers.models import Customer
from apps.discounts.models import Discount, DiscountCode
from apps.gift_cards.models import GiftCard, GiftCardTransaction
from apps.identity.models import User
from apps.inventory.models import InventoryHistory, InventoryItem, Location, Transfer, TransferItem
from apps.orders.models import Order, OrderItem
from apps.payments.models import Payment, PaymentTransaction
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

    def test_ledgers_can_be_viewed_but_not_added_edited_or_deleted(self):
        payment = Payment.objects.create(store=self.a, order=self.a.order, currency="INR", amount=5,
                                         status="captured", provider="manual", is_deleted=False)
        card = GiftCard.objects.create(store=self.a, code="GC-1", initial_balance=5, current_balance=4,
                                       currency_code="INR", status="active", is_deleted=False)
        rows = [
            PaymentTransaction.objects.create(payment=payment, transaction_type="capture", amount=5, status="success"),
            GiftCardTransaction.objects.create(gift_card=card, transaction_type="debit", amount=-1,
                                               balance_before=5, balance_after=4),
            InventoryHistory.objects.create(store=self.a, variant=self.a.variant, location=self.a.location,
                                            reason="cycle_count", source="admin", before=0, after=1, change=1,
                                            committed=0, unavailable=0, on_hand=1, incoming=0),
        ]
        for row in rows:
            model = type(row)
            with self.subTest(model=model._meta.label):
                self.assertEqual(self.client.get(self.url(model, "changelist")).status_code, 200)
                self.assertEqual(self.client.get(self.url(model, "change", row)).status_code, 200)
                self.assertEqual(self.client.get(self.url(model, "add")).status_code, 403)
                self.assertEqual(self.client.post(self.url(model, "change", row), {}).status_code, 403)
                self.assertEqual(self.client.post(self.url(model, "delete", row), {"post": "yes"}).status_code, 403)
                self.assertTrue(model.objects.filter(pk=row.pk).exists())

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

    def test_stock_levels_can_be_viewed_but_not_added_edited_or_deleted(self):
        stock = InventoryItem.objects.create(variant=self.a.variant, location=self.a.location, unavailable=0,
                                             committed=0, available=1, on_hand=1, incoming=0)
        numbers = {"variant": self.a.variant.pk, "location": self.a.location.pk, "available": 9, "committed": 0,
                   "unavailable": 0, "on_hand": 9, "incoming": 0}
        self.assertEqual(self.client.get(self.url(InventoryItem, "changelist")).status_code, 200)
        self.assertEqual(self.client.get(self.url(InventoryItem, "change", stock)).status_code, 200)
        self.assertEqual(self.client.get(self.url(InventoryItem, "add")).status_code, 403)
        self.assertEqual(self.post(InventoryItem, numbers).status_code, 403)
        self.assertEqual(self.post(InventoryItem, numbers, stock).status_code, 403)
        self.assertEqual(self.client.post(self.url(InventoryItem, "delete", stock), {"post": "yes"}).status_code, 403)
        self.client.post(self.url(InventoryItem, "changelist"),
                         {"action": "delete_selected", "_selected_action": [stock.pk], "post": "yes"})
        stock.refresh_from_db()
        self.assertEqual((stock.available, stock.on_hand), (1, 1))
        self.assertEqual(InventoryItem.objects.count(), 1)
        self.assertFalse(InventoryHistory.objects.exists())

    def transfer(self, status, quantity=1):
        transfer = Transfer.objects.create(store=self.a, transfer_number=f"T-{status}", status=status,
                                           source_location=self.a.location,
                                           destination_location=Location.objects.create(store=self.a, name="Shop"))
        item = TransferItem.objects.create(transfer=transfer, variant=self.a.variant, quantity=quantity)
        return transfer, item

    def test_transfer_status_cannot_be_set_in_admin(self):
        stock = InventoryItem.objects.create(variant=self.a.variant, location=self.a.location, unavailable=0,
                                             committed=0, available=5, on_hand=5, incoming=0)
        pending, _ = self.transfer("pending", quantity=2)
        r = self.client.get(self.url(Transfer, "change", pending))
        self.assertNotContains(r, 'name="status"')
        r = self.post(Transfer, self.form_data(pending, status="completed"), pending)  # crafted POST
        self.assertEqual(r.status_code, 302)
        pending.refresh_from_db()
        self.assertEqual(pending.status, "pending")

        # a new transfer starts pending whatever the POST says
        r = self.post(Transfer, {"store": self.a.pk, "transfer_number": "T-new", "status": "completed",
                                 "source_location": self.a.location.pk,
                                 "destination_location": pending.destination_location_id})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Transfer.objects.get(transfer_number="T-new").status, "pending")

        # no stock moved, nothing recorded
        stock.refresh_from_db()
        self.assertEqual((stock.available, stock.on_hand), (5, 5))
        self.assertFalse(InventoryItem.objects.exclude(pk=stock.pk).exists())
        self.assertFalse(InventoryHistory.objects.exists())

    def test_finished_transfers_and_their_items_cannot_change(self):
        for status in ("completed", "cancelled"):
            transfer, item = self.transfer(status)
            with self.subTest(status=status):
                self.assertEqual(self.client.get(self.url(Transfer, "change", transfer)).status_code, 200)
                self.assertEqual(self.post(Transfer, {"transfer_number": "X"}, transfer).status_code, 403)
                self.assertEqual(self.client.post(self.url(Transfer, "delete", transfer), {"post": "yes"}).status_code,
                                 403)
                self.assertEqual(self.post(TransferItem, {"variant": self.a.variant.pk, "quantity": 9}, item).status_code,
                                 403)
                self.assertEqual(self.client.post(self.url(TransferItem, "delete", item), {"post": "yes"}).status_code,
                                 403)
                r = self.post(TransferItem, {"transfer": transfer.pk, "variant": self.a.variant.pk, "quantity": 3})
                self.assertIn("already", str(self.errors(r)["__all__"]))
                transfer.refresh_from_db()
                item.refresh_from_db()
                self.assertEqual((transfer.status, transfer.transfer_number, item.quantity), (status, f"T-{status}", 1))
                self.assertEqual(transfer.items.count(), 1)

        # a pending transfer's items can still be edited
        _, item = self.transfer("pending")
        r = self.post(TransferItem, self.form_data(item, quantity=4), item)
        self.assertEqual(r.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.quantity, 4)
