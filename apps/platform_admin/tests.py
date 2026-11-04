"""
API keys, feature flags and platform administration.

    python manage.py test apps.platform_admin --settings=config.test_settings
"""
from datetime import timedelta

from django.core.cache import cache
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.audit.models import AuditLog
from apps.core.tests.test_rbac import RbacTestBase
from apps.platform_admin import flags
from apps.platform_admin.models import ApiKey, FeatureFlag

User = get_user_model()


class Base(RbacTestBase):
    def setUp(self):
        super().setUp()
        cache.clear()

    def make_key(self, scopes=("products.view",), name="Sync", **extra):
        self.as_owner()
        return self.ok("post", "/api/v1/api-keys/", {"name": name, "scopes": list(scopes), **extra}, expect=201)

    def key_client(self, key, header="bearer"):
        client = APIClient()
        if header == "bearer":
            client.credentials(HTTP_AUTHORIZATION=f"Bearer {key}")
        else:
            client.credentials(HTTP_X_API_KEY=key)
        return client

    def admin(self, email="root@example.com"):
        """A platform administrator; returns their access token."""
        token, _ = self.sign_up(email, "Root store")
        User.objects.filter(email=email).update(is_owner=True)
        return token

    def as_admin(self, token):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")


class ApiKeyLifecycleTest(Base):
    def test_secret_is_shown_once_and_only_a_hash_is_stored(self):
        created = self.make_key()
        self.assertTrue(created["key"].startswith("tssk_"))
        self.assertEqual(created["prefix"], created["key"].split("_")[1])
        self.assertEqual(created["status"], "active")

        row = ApiKey.objects.get(pk=created["id"])
        self.assertNotIn(created["key"], row.secret_hash)
        self.assertEqual(len(row.secret_hash), 64)

        listed = self.ok("get", "/api/v1/api-keys/")
        self.assertEqual(len(listed), 1)
        self.assertNotIn("key", listed[0])
        self.assertNotIn("secretHash", listed[0])

    def test_creation_is_validated(self):
        self.as_owner()
        self.ok("post", "/api/v1/api-keys/", {"name": "", "scopes": ["products.view"]}, expect=400)
        self.ok("post", "/api/v1/api-keys/", {"name": "x", "scopes": []}, expect=400)
        self.ok("post", "/api/v1/api-keys/", {"name": "x", "scopes": ["nope.view"]}, expect=400)
        # People-level access can't be handed to a key
        for scope in ("members.manage", "roles.manage", "billing.manage", "api_keys.manage", "payments.manage"):
            self.ok("post", "/api/v1/api-keys/", {"name": "x", "scopes": [scope]}, expect=400)
        past = (timezone.now() - timedelta(days=1)).isoformat()
        self.ok("post", "/api/v1/api-keys/", {"name": "x", "scopes": ["products.view"], "expiresAt": past}, expect=400)

    def test_manage_scope_brings_view_with_it(self):
        created = self.make_key(scopes=["products.manage"])
        self.assertEqual(created["scopes"], ["products.manage", "products.view"])

    def test_rename_and_change_scopes(self):
        created = self.make_key()
        updated = self.ok("patch", f"/api/v1/api-keys/{created['id']}/", {"name": "Renamed", "scopes": ["orders.view"]})
        self.assertEqual(updated["name"], "Renamed")
        self.assertEqual(updated["scopes"], ["orders.view"])
        self.assertNotIn("key", updated)

    def test_scope_catalog(self):
        self.as_owner()
        catalog = self.ok("get", "/api/v1/api-keys/scopes/")
        keys = {s["key"] for s in catalog}
        self.assertIn("products.view", keys)
        self.assertNotIn("members.manage", keys)
        self.assertNotIn("api_keys.manage", keys)

    def test_revoked_key_stops_working_and_cannot_be_changed_or_rotated(self):
        created = self.make_key()
        self.assertEqual(self.key_client(created["key"]).get("/api/v1/catalog/products/").status_code, 200)

        self.as_owner()
        revoked = self.ok("post", f"/api/v1/api-keys/{created['id']}/revoke/")
        self.assertEqual(revoked["status"], "revoked")
        self.assertEqual(self.key_client(created["key"]).get("/api/v1/catalog/products/").status_code, 401)

        self.ok("patch", f"/api/v1/api-keys/{created['id']}/", {"name": "x"}, expect=400)
        self.ok("post", f"/api/v1/api-keys/{created['id']}/rotate/", expect=400)

    def test_rotate_replaces_the_secret(self):
        created = self.make_key()
        self.as_owner()
        rotated = self.ok("post", f"/api/v1/api-keys/{created['id']}/rotate/")
        self.assertNotEqual(rotated["key"], created["key"])
        self.assertEqual(self.key_client(created["key"]).get("/api/v1/catalog/products/").status_code, 401)
        self.assertEqual(self.key_client(rotated["key"]).get("/api/v1/catalog/products/").status_code, 200)

    def test_expired_key_is_refused(self):
        created = self.make_key(expiresAt=(timezone.now() + timedelta(days=1)).isoformat())
        ApiKey.objects.filter(pk=created["id"]).update(expires_at=timezone.now() - timedelta(seconds=1))
        r = self.key_client(created["key"]).get("/api/v1/catalog/products/")
        self.assertEqual(r.status_code, 401)
        self.as_owner()
        self.assertEqual(self.ok("get", "/api/v1/api-keys/")[0]["status"], "expired")

    def test_delete_removes_the_row(self):
        created = self.make_key()
        self.as_owner()
        self.ok("delete", f"/api/v1/api-keys/{created['id']}/", expect=204)
        self.assertFalse(ApiKey.objects.filter(pk=created["id"]).exists())

    def test_a_store_has_a_limit_on_active_keys(self):
        from apps.platform_admin.serializers import MAX_ACTIVE_KEYS

        self.as_owner()
        for i in range(MAX_ACTIVE_KEYS):
            self.ok("post", "/api/v1/api-keys/", {"name": f"k{i}", "scopes": ["products.view"]}, expect=201)
        self.ok("post", "/api/v1/api-keys/", {"name": "one more", "scopes": ["products.view"]}, expect=400)


class ApiKeyAuthTest(Base):
    def test_read_only_key(self):
        created = self.make_key(scopes=["products.view"])
        client = self.key_client(created["key"])
        self.assertEqual(client.get("/api/v1/catalog/products/").status_code, 200)
        self.assertEqual(client.post("/api/v1/catalog/products/", {"title": "A", "sku": "A", "price": "5"}, format="json").status_code, 403)
        self.assertEqual(client.get("/api/v1/orders/").status_code, 403)

    def test_x_api_key_header_works_too(self):
        created = self.make_key()
        self.assertEqual(self.key_client(created["key"], header="x").get("/api/v1/catalog/products/").status_code, 200)

    def test_write_key_creates_data_and_the_audit_log_names_the_key(self):
        created = self.make_key(scopes=["products.manage"])
        client = self.key_client(created["key"])
        r = client.post("/api/v1/catalog/products/", {"title": "Via key", "sku": "VK-1", "price": "5"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)

        entry = AuditLog.objects.filter(store_id=self.store_id, action="products.create").first()
        self.assertEqual(entry.actor_email, "owner@example.com")
        self.assertEqual(entry.metadata["apiKey"], created["prefix"])

    def test_key_cannot_reach_account_team_or_key_endpoints(self):
        created = self.make_key(scopes=["products.manage", "orders.manage"])
        client = self.key_client(created["key"])
        for url in ("/api/v1/auth/me/", "/api/v1/members/", "/api/v1/roles/", "/api/v1/api-keys/",
                    "/api/v1/stores/", "/api/v1/audit/logs/", "/api/v1/platform/overview/"):
            self.assertIn(client.get(url).status_code, (401, 403), url)

    def test_key_does_not_carry_the_creators_platform_admin_powers(self):
        token = self.admin()
        self.as_admin(token)
        store = self.ok("post", "/api/v1/stores/", {"storeName": "Admin shop"}, expect=201)
        self.use(token, store["storeId"])
        key = self.ok("post", "/api/v1/api-keys/", {"name": "k", "scopes": ["products.view"]}, expect=201)

        client = self.key_client(key["key"])
        self.assertEqual(client.get("/api/v1/catalog/products/").status_code, 200)
        self.assertEqual(client.get("/api/v1/platform/overview/").status_code, 403)
        # Aimed at another store, the key is refused rather than redirected
        r = client.get("/api/v1/catalog/products/", HTTP_X_STORE_ID=self.store_id)
        self.assertEqual(r.status_code, 403)

    def test_malformed_and_unknown_keys(self):
        created = self.make_key()
        self.assertEqual(self.key_client("tssk_garbage").get("/api/v1/catalog/products/").status_code, 401)
        wrong = created["key"][:-4] + "AAAA"
        self.assertEqual(self.key_client(wrong).get("/api/v1/catalog/products/").status_code, 401)
        self.assertEqual(self.key_client("tssk_" + "a" * 8 + "_" + "b" * 30).get("/api/v1/catalog/products/").status_code, 401)

    def test_usage_is_recorded(self):
        created = self.make_key()
        self.key_client(created["key"]).get("/api/v1/catalog/products/")
        row = ApiKey.objects.get(pk=created["id"])
        self.assertIsNotNone(row.last_used_at)

    def test_rate_limit(self):
        created = self.make_key()
        client = self.key_client(created["key"])
        with self.settings(API_KEY_RATE_LIMIT_PER_MINUTE=2):
            codes = [client.get("/api/v1/catalog/products/").status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])

    def test_deleted_creator_leaves_an_unusable_key(self):
        created = self.make_key()
        ApiKey.objects.filter(pk=created["id"]).update(created_by=None)
        self.assertEqual(self.key_client(created["key"]).get("/api/v1/catalog/products/").status_code, 401)


class ApiKeyAccessTest(Base):
    def test_staff_without_the_permission_cannot_see_or_make_keys(self):
        self.staff("merch@example.com", "merchandiser")
        self.ok("get", "/api/v1/api-keys/", expect=403)
        self.ok("post", "/api/v1/api-keys/", {"name": "x", "scopes": ["products.view"]}, expect=403)

    def test_staff_cannot_give_a_key_more_than_they_hold(self):
        self.as_owner()
        role = self.ok("post", "/api/v1/roles/", {
            "name": "Key maker", "permissions": ["api_keys.manage", "products.view"],
        }, expect=201)
        token, _ = self.sign_up("maker@example.com", "Maker store")
        self.as_owner()
        self.ok("post", "/api/v1/members/", {"email": "maker@example.com", "roleIds": [role["id"]]}, expect=201)
        self.use(token, self.store_id)

        self.ok("post", "/api/v1/api-keys/", {"name": "mine", "scopes": ["products.view"]}, expect=201)
        self.ok("post", "/api/v1/api-keys/", {"name": "too much", "scopes": ["orders.manage"]}, expect=400)

    def test_keys_are_private_to_their_store(self):
        created = self.make_key()
        other_token, other = self.sign_up("other@example.com", "Other shop")
        self.assertEqual(self.ok("get", "/api/v1/api-keys/"), [])
        self.ok("get", f"/api/v1/api-keys/{created['id']}/", expect=404)
        self.ok("post", f"/api/v1/api-keys/{created['id']}/revoke/", expect=404)

    def test_key_changes_are_audited_without_the_secret(self):
        created = self.make_key()
        self.as_owner()
        entry = AuditLog.objects.filter(store_id=self.store_id, action="api_keys.create").first()
        self.assertIsNotNone(entry)
        self.assertNotIn(created["key"], str(entry.changes))
        fields = entry.changes[0]["fields"]
        self.assertEqual(fields["secret_hash"]["to"], "[redacted]")

    def test_feature_flag_can_switch_the_feature_off_for_one_store(self):
        admin = self.admin()
        self.as_owner()
        self.ok("get", "/api/v1/api-keys/")

        self.as_admin(admin)
        self.ok("post", "/api/v1/platform/feature-flags/", {"key": "api_keys", "name": "API keys", "enabled": True}, expect=201)
        self.ok("put", f"/api/v1/platform/feature-flags/api_keys/overrides/{self.store_id}/", {"enabled": False})

        self.as_owner()
        self.ok("get", "/api/v1/api-keys/", expect=403)
        self.assertFalse(self.ok("get", "/api/v1/feature-flags/")["flags"]["api_keys"])

        self.as_admin(admin)
        self.ok("delete", f"/api/v1/platform/feature-flags/api_keys/overrides/{self.store_id}/", expect=204)
        self.as_owner()
        self.ok("get", "/api/v1/api-keys/")


class FeatureFlagTest(Base):
    def test_unknown_flag_is_off_and_registry_default_applies(self):
        self.assertFalse(flags.is_enabled("brand_new"))
        self.assertTrue(flags.is_enabled("api_keys"))  # on by default until a row says otherwise

    def test_flag_row_switches_and_rollout(self):
        flag = FeatureFlag.objects.create(key="beta", name="Beta", enabled=False)
        self.assertFalse(flags.is_enabled("beta", self.store_id))
        flag.enabled = True
        flag.save()
        self.assertTrue(flags.is_enabled("beta", self.store_id))

        flag.rollout_percent = 0
        flag.save()
        self.assertFalse(flags.is_enabled("beta", self.store_id))

    def test_rollout_is_stable_and_proportional(self):
        import uuid

        ids = [uuid.uuid4() for _ in range(400)]
        first = [flags.bucket("beta", i) < 30 for i in ids]
        self.assertEqual(first, [flags.bucket("beta", i) < 30 for i in ids])
        share = sum(first) / len(ids)
        self.assertTrue(0.2 < share < 0.4, share)
        # Growing the rollout never switches a store off
        self.assertTrue(all(b < 60 for b, was in zip((flags.bucket("beta", i) for i in ids), first) if was))

    def test_override_beats_everything(self):
        admin = self.admin()
        self.as_admin(admin)
        self.ok("post", "/api/v1/platform/feature-flags/", {"key": "beta", "name": "Beta", "enabled": False}, expect=201)
        self.ok("put", f"/api/v1/platform/feature-flags/beta/overrides/{self.store_id}/", {"enabled": True})
        self.as_owner()
        self.assertTrue(self.ok("get", "/api/v1/feature-flags/")["flags"]["beta"])

    def test_every_member_can_read_flags_even_without_roles(self):
        self.staff("nobody@example.com")
        self.assertIn("api_keys", self.ok("get", "/api/v1/feature-flags/")["flags"])


class PlatformAccessTest(Base):
    URLS = [
        "/api/v1/platform/overview/", "/api/v1/platform/stores/", "/api/v1/platform/users/",
        "/api/v1/platform/api-keys/", "/api/v1/platform/feature-flags/", "/api/v1/platform/audit/",
    ]

    def test_ordinary_users_and_store_owners_are_refused(self):
        self.as_owner()
        for url in self.URLS:
            self.assertEqual(self.client.get(url).status_code, 403, url)

    def test_signed_out_is_refused(self):
        anonymous = APIClient()  # self.client still holds the session cookie set at sign-up
        for url in self.URLS:
            self.assertEqual(anonymous.get(url).status_code, 401, url)

    def test_platform_admin_can_read_everything(self):
        self.as_admin(self.admin())
        for url in self.URLS:
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_ordinary_users_cannot_write(self):
        self.as_owner()
        for method, url, body in [
            ("post", f"/api/v1/platform/stores/{self.store_id}/suspend/", {}),
            ("post", "/api/v1/platform/feature-flags/", {"key": "xx", "name": "x"}),
            ("put", f"/api/v1/platform/feature-flags/api_keys/overrides/{self.store_id}/", {"enabled": False}),
        ]:
            self.assertEqual(getattr(self.client, method)(url, body, format="json").status_code, 403, url)


class PlatformStoresTest(Base):
    def test_store_lists_show_every_store_to_the_admin(self):
        # The platform admin's own store is "Root store"; "Shop" belongs to owner@example.com
        self.as_admin(self.admin())

        for listed in (self.ok("get", "/api/v1/auth/me/")["stores"], self.ok("get", "/api/v1/stores/")):
            self.assertEqual({s["storeName"] for s in listed}, {"Shop", "Root store"})

        # The platform page lists every store too, and a store can be opened by its id
        everything = self.ok("get", "/api/v1/platform/stores/")
        self.assertEqual({s["storeName"] for s in everything["results"]}, {"Shop", "Root store"})
        self.ok("get", f"/api/v1/stores/{self.store_id}/")

    def test_a_normal_owner_never_sees_other_stores(self):
        self.admin()
        self.as_owner()
        self.assertEqual([s["storeName"] for s in self.ok("get", "/api/v1/stores/")], ["Shop"])
        self.assertEqual([s["storeName"] for s in self.ok("get", "/api/v1/auth/me/")["stores"]], ["Shop"])

    def test_overview(self):
        self.admin()
        self.make_key()
        token = self.admin("second@example.com")
        self.as_admin(token)
        data = self.ok("get", "/api/v1/platform/overview/")
        self.assertEqual(data["stores"]["total"], 3)
        self.assertEqual(data["stores"]["active"], 3)
        self.assertEqual(data["users"]["platformAdmins"], 2)
        self.assertEqual(data["apiKeys"]["active"], 1)

    def test_list_search_and_detail(self):
        self.as_admin(self.admin())
        found = self.ok("get", "/api/v1/platform/stores/?q=shop")
        self.assertEqual([s["storeName"] for s in found["results"]], ["Shop"])
        self.assertEqual(found["results"][0]["memberCount"], 1)

        detail = self.ok("get", f"/api/v1/platform/stores/{self.store_id}/")
        self.assertEqual(detail["members"][0]["email"], "owner@example.com")
        self.assertIn("api_keys", detail["flags"])
        self.ok("get", "/api/v1/platform/stores/00000000-0000-0000-0000-000000000000/", expect=404)

    def test_suspend_blocks_the_store_and_unsuspend_restores_it(self):
        created = self.make_key(scopes=["products.manage"])
        admin = self.admin()
        self.as_admin(admin)
        suspended = self.ok("post", f"/api/v1/platform/stores/{self.store_id}/suspend/", {"reason": "Chargebacks"})
        self.assertEqual(suspended["status"], "suspended")

        self.as_owner()
        self.ok("get", "/api/v1/catalog/products/", expect=403)
        self.assertEqual(self.key_client(created["key"]).get("/api/v1/catalog/products/").status_code, 403)
        # The owner still sees the store (and its status) in their list
        stores = self.ok("get", "/api/v1/stores/")
        self.assertEqual([s["status"] for s in stores if s["storeId"] == self.store_id], ["suspended"])

        # A platform admin can still get in, to look at what is wrong
        self.use(admin, self.store_id)
        self.ok("get", "/api/v1/catalog/products/")

        self.as_admin(admin)
        self.ok("post", f"/api/v1/platform/stores/{self.store_id}/unsuspend/")
        self.as_owner()
        self.ok("get", "/api/v1/catalog/products/")
        self.assertEqual(self.key_client(created["key"]).get("/api/v1/catalog/products/").status_code, 200)

    def test_suspension_is_audited_in_the_stores_own_log(self):
        self.as_admin(self.admin())
        self.ok("post", f"/api/v1/platform/stores/{self.store_id}/suspend/", {"reason": "Fraud review"})
        entry = AuditLog.objects.get(store_id=self.store_id, action="platform.store_suspended")
        self.assertEqual(entry.actor_email, "root@example.com")
        self.assertEqual(entry.metadata["reason"], "Fraud review")
        # The owner is locked out meanwhile; once restored, the entry is in their log
        self.ok("post", f"/api/v1/platform/stores/{self.store_id}/unsuspend/")
        self.as_owner()
        actions = [e["action"] for e in self.ok("get", "/api/v1/audit/logs/?action=platform.*")["results"]]
        self.assertEqual(set(actions), {"platform.store_suspended", "platform.store_unsuspended"})

    def test_closed_store_cannot_be_suspended(self):
        self.as_admin(self.admin())
        self.ok("delete", f"/api/v1/stores/{self.store_id}/", expect=204)
        self.ok("post", f"/api/v1/platform/stores/{self.store_id}/suspend/", {}, expect=400)


class PlatformUsersTest(Base):
    def test_list_search_and_detail(self):
        self.as_admin(self.admin())
        found = self.ok("get", "/api/v1/platform/users/?q=owner@")
        self.assertEqual([u["email"] for u in found["results"]], ["owner@example.com"])
        user_id = found["results"][0]["id"]
        detail = self.ok("get", f"/api/v1/platform/users/{user_id}/")
        self.assertEqual(detail["stores"][0]["storeName"], "Shop")
        self.assertEqual(self.ok("get", "/api/v1/platform/users/?admin=true")["count"], 1)

    def test_grant_and_revoke_platform_admin(self):
        self.as_admin(self.admin())
        owner = User.objects.get(email="owner@example.com")
        url = f"/api/v1/platform/users/{owner.pk}/platform-admin/"
        self.assertTrue(self.ok("post", url, {"isPlatformAdmin": True})["isPlatformAdmin"])
        self.assertTrue(User.objects.get(pk=owner.pk).is_owner)
        self.assertTrue(AuditLog.objects.filter(action="platform.admin_granted", object_repr="owner@example.com").exists())

        self.assertFalse(self.ok("post", url, {"isPlatformAdmin": False})["isPlatformAdmin"])
        self.assertFalse(User.objects.get(pk=owner.pk).is_owner)
        self.ok("post", url, {"isPlatformAdmin": "yes"}, expect=400)

    def test_cannot_remove_your_own_access_or_the_last_admin(self):
        self.as_admin(self.admin())
        me = User.objects.get(email="root@example.com")
        self.ok("post", f"/api/v1/platform/users/{me.pk}/platform-admin/", {"isPlatformAdmin": False}, expect=400)

        second = self.admin("second@example.com")
        self.as_admin(second)
        # Two admins: one may demote the other, leaving one, who then can't remove themselves
        self.ok("post", f"/api/v1/platform/users/{me.pk}/platform-admin/", {"isPlatformAdmin": False})
        self.assertEqual(User.objects.filter(is_owner=True).count(), 1)
        other = User.objects.get(email="second@example.com")
        self.ok("post", f"/api/v1/platform/users/{other.pk}/platform-admin/", {"isPlatformAdmin": False}, expect=400)

    def test_new_admin_gets_platform_access_straight_away(self):
        self.as_owner()
        self.ok("get", "/api/v1/platform/overview/", expect=403)
        self.as_admin(self.admin())
        owner = User.objects.get(email="owner@example.com")
        self.ok("post", f"/api/v1/platform/users/{owner.pk}/platform-admin/", {"isPlatformAdmin": True})
        self.as_owner()
        self.ok("get", "/api/v1/platform/overview/")


class PlatformApiKeysTest(Base):
    def test_list_filter_and_revoke_across_stores(self):
        created = self.make_key(name="Warehouse")
        self.make_key(name="Other")
        admin = self.admin()
        self.as_admin(admin)

        everything = self.ok("get", "/api/v1/platform/api-keys/")
        self.assertEqual(everything["count"], 2)
        self.assertNotIn("key", everything["results"][0])
        self.assertEqual(self.ok("get", "/api/v1/platform/api-keys/?q=ware")["count"], 1)
        self.assertEqual(self.ok("get", f"/api/v1/platform/api-keys/?store={self.store_id}")["count"], 2)

        revoked = self.ok("post", f"/api/v1/platform/api-keys/{created['id']}/revoke/")
        self.assertEqual(revoked["status"], "revoked")
        self.assertEqual(self.ok("get", "/api/v1/platform/api-keys/?status=revoked")["count"], 1)
        self.assertEqual(self.ok("get", "/api/v1/platform/api-keys/?status=active")["count"], 1)
        self.assertEqual(self.key_client(created["key"]).get("/api/v1/catalog/products/").status_code, 401)
        self.assertTrue(AuditLog.objects.filter(action="platform.api_key_revoked", store_id=self.store_id).exists())


class PlatformFlagsTest(Base):
    def test_create_list_update_delete(self):
        self.as_admin(self.admin())
        created = self.ok("post", "/api/v1/platform/feature-flags/", {
            "key": "new_checkout", "name": "New checkout", "description": "Try it", "enabled": True, "rolloutPercent": 25,
        }, expect=201)
        self.assertEqual(created["rolloutPercent"], 25)

        listed = {f["key"]: f for f in self.ok("get", "/api/v1/platform/feature-flags/")}
        self.assertTrue(listed["new_checkout"]["saved"])
        self.assertFalse(listed["api_keys"]["saved"])  # code-defined, no row yet

        patched = self.ok("patch", "/api/v1/platform/feature-flags/new_checkout/", {"rolloutPercent": 50, "enabled": False})
        self.assertEqual((patched["rolloutPercent"], patched["enabled"]), (50, False))
        self.ok("delete", "/api/v1/platform/feature-flags/new_checkout/", expect=204)
        self.ok("get", "/api/v1/platform/feature-flags/new_checkout/", expect=404)
        actions = set(AuditLog.objects.filter(action__startswith="platform.flag").values_list("action", flat=True))
        self.assertEqual(actions, {"platform.flag_created", "platform.flag_updated", "platform.flag_deleted"})

    def test_validation(self):
        self.as_admin(self.admin())
        self.ok("post", "/api/v1/platform/feature-flags/", {"key": "Bad Key", "name": "x"}, expect=400)
        self.ok("post", "/api/v1/platform/feature-flags/", {"key": "ok_key", "name": "x", "rolloutPercent": 101}, expect=400)
        self.ok("post", "/api/v1/platform/feature-flags/", {"key": "ok_key", "name": "x"}, expect=201)
        self.ok("post", "/api/v1/platform/feature-flags/", {"key": "ok_key", "name": "again"}, expect=400)
        self.ok("patch", "/api/v1/platform/feature-flags/ok_key/", {"key": "other"}, expect=400)

    def test_overrides_list_set_remove(self):
        self.as_admin(self.admin())
        self.ok("post", "/api/v1/platform/feature-flags/", {"key": "beta", "name": "Beta"}, expect=201)
        self.ok("put", f"/api/v1/platform/feature-flags/beta/overrides/{self.store_id}/", {"enabled": True})
        self.ok("put", f"/api/v1/platform/feature-flags/beta/overrides/{self.store_id}/", {"enabled": "maybe"}, expect=400)
        overrides = self.ok("get", "/api/v1/platform/feature-flags/beta/overrides/")
        self.assertEqual([(o["storeName"], o["enabled"]) for o in overrides], [("Shop", True)])
        self.assertEqual(self.ok("get", "/api/v1/platform/feature-flags/beta/")["overrideCount"], 1)

        self.ok("delete", f"/api/v1/platform/feature-flags/beta/overrides/{self.store_id}/", expect=204)
        self.assertEqual(self.ok("get", "/api/v1/platform/feature-flags/beta/overrides/"), [])
        self.ok("put", "/api/v1/platform/feature-flags/beta/overrides/00000000-0000-0000-0000-000000000000/", {"enabled": True}, expect=404)


class PlatformAuditTest(Base):
    def test_sees_every_store_and_filters(self):
        self.make_key()
        self.as_admin(self.admin())
        self.ok("post", f"/api/v1/platform/stores/{self.store_id}/suspend/", {})
        everything = self.ok("get", "/api/v1/platform/audit/")
        self.assertGreaterEqual(everything["count"], 2)
        self.assertTrue(all("store" in e for e in everything["results"]))

        only = self.ok("get", "/api/v1/platform/audit/?platformOnly=true")
        self.assertEqual({e["action"] for e in only["results"]}, {"platform.store_suspended"})
        self.assertEqual(only["results"][0]["store"], "Shop")
        self.assertEqual(self.ok("get", "/api/v1/platform/audit/?store=00000000-0000-0000-0000-000000000000")["count"], 0)
