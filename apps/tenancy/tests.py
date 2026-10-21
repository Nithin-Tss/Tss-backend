"""
Multi-store tests.

    python manage.py test apps.tenancy --settings=config.test_settings
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils.text import slugify
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase

from apps.core.tests.test_flow import StoreClientMixin
from apps.themes.models import StoreTheme, Theme, ThemeFile
from apps.themes.services import get_store_theme

from .models import Store, StoreMembership
from .services import close_store, create_store, delete_store, reopen_store


class MultiStoreTest(StoreClientMixin, APITestCase):
    def test_one_user_many_stores(self):
        token, first = self.sign_up("owner@example.com", "First store")
        second = self.ok("post", "/api/v1/stores/", {"storeName": "Second store"}, expect=201)

        listed = self.ok("get", "/api/v1/stores/")
        self.assertEqual([s["storeName"] for s in listed], ["First store", "Second store"])
        self.assertEqual({s["role"] for s in listed}, {"owner"})

        # Each store only sees its own products
        self.ok("post", "/api/v1/catalog/products/", {"title": "Only in first", "sku": "SKU-ONLY-IN-FIRST", "price": "5"}, expect=201)
        self.use(token, second["storeId"])
        self.assertEqual(self.ok("get", "/api/v1/catalog/products/"), [])
        self.use(token, first["storeId"])
        self.assertEqual(len(self.ok("get", "/api/v1/catalog/products/")), 1)

    def test_rename_and_close(self):
        token, store = self.sign_up("owner@example.com", "Old name")
        url = f"/api/v1/stores/{store['storeId']}/"

        renamed = self.ok("patch", url, {"storeName": "  New name  "})
        self.assertEqual(renamed["storeName"], "New name")
        self.ok("patch", url, {"storeName": " "}, expect=400)

        self.ok("delete", url, expect=204)
        self.assertEqual(self.ok("get", "/api/v1/stores/"), [])
        self.assertEqual(Store.objects.get(pk=store["storeId"]).status, "closed")

        # The closed store's data can't be reached any more
        self.ok("get", "/api/v1/catalog/products/", expect=403)

    def test_only_owner_can_rename_or_close(self):
        _, store = self.sign_up("owner@example.com", "Owned")
        url = f"/api/v1/stores/{store['storeId']}/"

        staff_token, _ = self.sign_up("staff@example.com", "Staff's own")
        StoreMembership.objects.create(
            user=get_user_model().objects.get(email="staff@example.com"),
            store_id=store["storeId"], role="staff", status="active",
        )
        self.use(staff_token, store["storeId"])

        self.ok("patch", url, {"storeName": "Hijacked"}, expect=403)
        self.ok("delete", url, expect=403)
        self.assertEqual(Store.objects.get(pk=store["storeId"]).store_name, "Owned")

    def make_admin(self, email):
        get_user_model().objects.filter(email=email).update(is_owner=True)

    def test_new_store_has_default_settings(self):
        _, store = self.sign_up("owner@example.com", "Defaults")
        self.assertEqual(store["currency"], "INR")
        self.assertEqual(store["unitSystem"], "metric")
        self.assertEqual(store["weightUnit"], "kg")
        self.assertEqual(store["orderPrefix"], "#")
        self.assertEqual(store["autoFulfill"], "gift_cards")
        self.assertTrue(store["autoArchive"])
        self.assertEqual(store["contactEmail"], "")

    def test_update_settings(self):
        _, store = self.sign_up("owner@example.com", "Settings")
        url = f"/api/v1/stores/{store['storeId']}/"

        saved = self.ok("patch", url, {
            "contactEmail": "hello@shop.test", "phone": "+91 98765 43210",
            "address": "  1  Main   Road ", "currency": "aud", "timezone": "Australia/Melbourne",
            "unitSystem": "imperial", "weightUnit": "lb", "backupRegion": "Australia",
            "orderPrefix": "SO-", "orderSuffix": "-X", "autoFulfill": "none", "autoArchive": False,
        })
        self.assertEqual(saved["currency"], "AUD")
        self.assertEqual(saved["address"], "1 Main Road")
        self.assertEqual(self.ok("get", url)["timezone"], "Australia/Melbourne")
        self.assertFalse(self.ok("get", url)["autoArchive"])

    def test_settings_are_validated(self):
        _, store = self.sign_up("owner@example.com", "Validation")
        url = f"/api/v1/stores/{store['storeId']}/"

        for bad in (
            {"contactEmail": "nope"}, {"phone": "abc"}, {"currency": "RUPEES"},
            {"timezone": "Mars/Base"}, {"unitSystem": "cubits"}, {"weightUnit": "stone"},
            {"autoFulfill": "sometimes"}, {"orderPrefix": "A1"}, {"orderSuffix": "9"},
        ):
            self.ok("patch", url, bad, expect=400)

    def test_order_numbers_follow_prefix_and_suffix(self):
        _, store = self.sign_up("owner@example.com", "Numbers")
        self.ok("patch", f"/api/v1/stores/{store['storeId']}/", {"orderPrefix": "SO-", "orderSuffix": "-X"})

        product = self.ok("post", "/api/v1/catalog/products/", {
            "title": "Tee", "status": "active", "price": "100", "sku": "TEE-1",
        }, expect=201)
        numbers = [
            self.ok("post", "/api/v1/orders/", {
                "items": [{"product": product["id"], "quantity": 1}],
            }, expect=201)["order_number"]
            for _ in range(2)
        ]
        self.assertEqual(numbers, ["SO-1001-X", "SO-1002-X"])

    def test_platform_admin_manages_any_store(self):
        _, store = self.sign_up("owner@example.com", "Someone's store")
        admin_token, _ = self.sign_up("admin@example.com", "Admin's own")
        self.make_admin("admin@example.com")
        self.use(admin_token, store["storeId"])
        url = f"/api/v1/stores/{store['storeId']}/"

        self.assertEqual(self.ok("patch", url, {"currency": "usd"})["currency"], "USD")
        self.ok("delete", url, expect=204)

    def test_only_platform_admin_reopens_a_store(self):
        owner_token, store = self.sign_up("owner@example.com", "Closed shop")
        url = f"/api/v1/stores/{store['storeId']}/"
        self.ok("delete", url, expect=204)

        # The owner of a closed store can't reopen it
        self.ok("post", url + "reopen/", expect=403)

        admin_token, _ = self.sign_up("admin@example.com", "Admin's own")
        self.make_admin("admin@example.com")
        reopened = self.ok("post", url + "reopen/")
        self.assertEqual(reopened["status"], "active")

        # ...and the owner has their access back
        self.use(owner_token, store["storeId"])
        self.assertEqual(self.ok("get", url)["storeName"], "Closed shop")
        self.ok("patch", url, {"phone": "12345"})

    def test_staff_cannot_change_settings_whatever_their_role(self):
        _, store = self.sign_up("owner@example.com", "Owned")
        url = f"/api/v1/stores/{store['storeId']}/"
        staff_token, _ = self.sign_up("staff@example.com", "Staff's own")
        StoreMembership.objects.create(
            user=get_user_model().objects.get(email="staff@example.com"),
            store_id=store["storeId"], role="staff", status="active",
        )
        self.use(staff_token, store["storeId"])

        self.assertEqual(self.ok("get", url)["storeName"], "Owned")  # can look
        self.ok("patch", url, {"currency": "USD"}, expect=403)
        self.ok("delete", url, expect=403)
        self.assertEqual(Store.objects.get(pk=store["storeId"]).currency, "INR")

    def test_cannot_touch_someone_elses_store(self):
        _, store_a = self.sign_up("a@example.com", "Store A")
        self.sign_up("b@example.com", "Store B")
        url = f"/api/v1/stores/{store_a['storeId']}/"

        self.ok("get", url, expect=404)
        self.ok("patch", url, {"storeName": "Mine now"}, expect=404)
        self.ok("patch", url, {"currency": "USD"}, expect=404)
        self.ok("delete", url, expect=404)
        self.ok("post", url + "reopen/", expect=403)


class DeleteStoreThemesTest(TestCase):
    """Permanent store deletion (services.delete_store) and what happens to themes."""

    def bare_store(self, name):
        # No members or other data, so only the theme rows are in the way of deleting it
        return Store.objects.create(store_name=name, store_slug=slugify(name), status="active")

    def shared_theme(self):
        theme = Theme.objects.create(name="Shared X", slug="shared-x", theme_data={})
        ThemeFile.objects.create(theme=theme, file_path="layout/theme.liquid", file_type="liquid",
                                 content="{{ sections }}", is_required=True, file_size=14)
        return theme

    def assign(self, store, theme, custom_data=None):
        return StoreTheme.objects.create(store=store, theme=theme, installed_version="1.0.0",
                                         custom_data=custom_data or {}, is_active=True)

    def test_private_theme_and_its_files_go_with_the_store(self):
        store_a = self.bare_store("Store A")
        theme = get_store_theme(store_a).theme  # Store A's own copy
        self.assertTrue(ThemeFile.objects.filter(theme=theme).exists())

        delete_store(store_a)

        self.assertFalse(Store.objects.filter(pk=store_a.pk).exists())
        self.assertFalse(StoreTheme.objects.filter(theme=theme).exists())
        self.assertFalse(Theme.objects.filter(pk=theme.pk).exists())
        self.assertFalse(ThemeFile.objects.filter(theme=theme).exists())

    def test_shared_theme_stays_for_the_other_store(self):
        store_a, store_b = self.bare_store("Store A"), self.bare_store("Store B")
        theme_x = self.shared_theme()
        self.assign(store_a, theme_x)
        b_row = self.assign(store_b, theme_x, {"settings": {"colors": "B's own"}})

        delete_store(store_a)

        self.assertFalse(StoreTheme.objects.filter(store_id=store_a.pk).exists())
        self.assertTrue(Theme.objects.filter(pk=theme_x.pk).exists())
        self.assertEqual(ThemeFile.objects.filter(theme=theme_x).count(), 1)
        b_row.refresh_from_db()
        self.assertEqual((b_row.theme_id, b_row.is_active, b_row.custom_data),
                         (theme_x.pk, True, {"settings": {"colors": "B's own"}}))

    def test_private_theme_still_used_by_another_store_is_kept(self):
        store_a, store_b = self.bare_store("Store A"), self.bare_store("Store B")
        theme_y = get_store_theme(store_a).theme
        files = ThemeFile.objects.filter(theme=theme_y).count()
        self.assign(store_b, theme_y)

        delete_store(store_a)

        self.assertTrue(Theme.objects.filter(pk=theme_y.pk).exists())
        self.assertEqual(ThemeFile.objects.filter(theme=theme_y).count(), files)
        self.assertTrue(StoreTheme.objects.filter(store=store_b, theme=theme_y).exists())

    def test_other_stores_own_themes_are_untouched(self):
        store_a, store_b = self.bare_store("Store A"), self.bare_store("Store B")
        get_store_theme(store_a)
        b_row = get_store_theme(store_b)
        b_row.custom_data = {"settings": {"heading_font": "lora"}}
        b_row.save(update_fields=["custom_data"])
        b_files = ThemeFile.objects.filter(theme=b_row.theme).count()

        delete_store(store_a)

        b_row.refresh_from_db()
        self.assertEqual(b_row.custom_data, {"settings": {"heading_font": "lora"}})
        self.assertTrue(Theme.objects.filter(pk=b_row.theme_id).exists())
        self.assertEqual(ThemeFile.objects.filter(theme=b_row.theme).count(), b_files)

    def test_close_and_reopen_keep_the_theme(self):
        owner = get_user_model().objects.create_user(email="owner@example.com", password="x-Strong-123")
        store = create_store(owner, "Closable")
        row = get_store_theme(store)

        close_store(store)
        self.assertTrue(StoreTheme.objects.filter(pk=row.pk, theme=row.theme).exists())
        reopen_store(store)

        store.refresh_from_db()
        self.assertEqual(store.status, "active")
        self.assertEqual(get_store_theme(store).pk, row.pk)
        self.assertTrue(ThemeFile.objects.filter(theme=row.theme).exists())

    def test_a_protected_store_is_refused_and_nothing_is_deleted(self):
        owner = get_user_model().objects.create_user(email="owner@example.com", password="x-Strong-123")
        store = create_store(owner, "Has members")  # its owner membership protects it
        row = get_store_theme(store)
        files = ThemeFile.objects.filter(theme=row.theme).count()

        with self.assertRaises(ValidationError) as refused:
            delete_store(store)

        self.assertIn("Store Memberships", str(refused.exception.detail["detail"]))
        # Rolled back as one unit: the theme cleanup that ran first is undone too
        self.assertTrue(Store.objects.filter(pk=store.pk).exists())
        self.assertTrue(StoreTheme.objects.filter(pk=row.pk).exists())
        self.assertTrue(Theme.objects.filter(pk=row.theme_id).exists())
        self.assertEqual(ThemeFile.objects.filter(theme=row.theme).count(), files)
