"""
Role-based access control tests.

    python manage.py test apps.core.tests.test_rbac --settings=config.test_settings
"""
from django.urls import get_resolver
from rest_framework.test import APITestCase

from apps.core import rbac
from apps.core.viewsets import StoreScopedGenericViewSet
from apps.tenancy.models import StoreMembership

from .test_flow import StoreClientMixin


class RbacTestBase(StoreClientMixin, APITestCase):
    def setUp(self):
        self.owner_token, self.store = self.sign_up("owner@example.com", "Shop")
        self.store_id = self.store["storeId"]
        self.roles = {r["key"] or r["name"]: r for r in self.ok("get", "/api/v1/roles/")}

    def staff(self, email, *role_keys):
        """A second account that is staff in the store with the given built-in roles."""
        token, _ = self.sign_up(email, f"{email} own store")
        self.use(self.owner_token, self.store_id)
        self.ok("post", "/api/v1/members/", {
            "email": email, "roleIds": [self.roles[k]["id"] for k in role_keys],
        }, expect=201)
        self.use(token, self.store_id)
        return token

    def as_owner(self):
        self.use(self.owner_token, self.store_id)


class PermissionCatalogTest(RbacTestBase):
    def test_there_are_29_permissions_in_13_areas(self):
        self.assertEqual(len(rbac.ALL_PERMISSIONS), 29)
        self.assertEqual(len(rbac.AREAS), 13)

    def test_every_store_view_declares_an_area(self):
        missing = []

        def walk(patterns):
            for p in patterns:
                if hasattr(p, "url_patterns"):
                    walk(p.url_patterns)
                    continue
                cls = getattr(p.callback, "cls", None)
                if cls is None:
                    continue
                perms = getattr(cls, "permission_classes", [])
                if any(c.__name__ == "HasStorePermission" for c in perms):
                    if not getattr(cls, "permission_resource", None):
                        missing.append(cls.__name__)

        walk(get_resolver().url_patterns)
        self.assertEqual(missing, [])
        self.assertIn("HasStorePermission", [c.__name__ for c in StoreScopedGenericViewSet.permission_classes])


class EnforcementTest(RbacTestBase):
    def test_owner_can_do_everything(self):
        self.ok("post", "/api/v1/catalog/products/", {"title": "A", "sku": "A-1", "price": "5"}, expect=201)

    def test_staff_without_roles_can_do_nothing(self):
        self.staff("nobody@example.com")
        self.ok("get", "/api/v1/catalog/products/", expect=403)
        self.ok("get", "/api/v1/orders/", expect=403)

    def test_merchandiser_manages_products_but_not_orders(self):
        self.staff("merch@example.com", "merchandiser")
        product = self.ok("post", "/api/v1/catalog/products/", {"title": "A", "sku": "A-1", "price": "5"}, expect=201)
        self.ok("get", "/api/v1/catalog/products/")
        self.ok("get", "/api/v1/orders/", expect=403)
        self.ok("delete", f"/api/v1/catalog/products/{product['id']}/", expect=204)

    def test_view_only_cannot_write(self):
        self.staff("support@example.com", "customer_support")
        self.ok("get", "/api/v1/catalog/products/")  # products.view
        self.ok("post", "/api/v1/catalog/products/", {"title": "A", "sku": "A-1", "price": "5"}, expect=403)

    def test_delete_needs_its_own_permission(self):
        self.as_owner()
        product = self.ok("post", "/api/v1/catalog/products/", {"title": "A", "sku": "A-1", "price": "5"}, expect=201)
        role = self.ok("post", "/api/v1/roles/", {
            "name": "Catalog editor", "permissions": ["products.view", "products.manage"],
        }, expect=201)
        self.roles["Catalog editor"] = role
        self.staff("editor@example.com", "Catalog editor")
        self.ok("delete", f"/api/v1/catalog/products/{product['id']}/", expect=403)

    def test_refund_needs_orders_refund(self):
        self.staff("support@example.com", "customer_support")
        self.ok("post", "/api/v1/payments/00000000-0000-0000-0000-000000000000/refund/", {}, expect=403)

    def test_themes_preview_only_needs_view(self):
        self.staff("viewer@example.com", "online_store_editor")
        self.ok("get", "/api/v1/themes/files/")

    def test_roles_add_up(self):
        self.staff("both@example.com", "merchandiser", "marketer")
        self.ok("get", "/api/v1/catalog/products/")
        self.ok("get", "/api/v1/discounts/")

    def test_removed_member_loses_access(self):
        self.staff("gone@example.com", "merchandiser")
        StoreMembership.objects.filter(user__email="gone@example.com", store_id=self.store_id).update(status="removed")
        self.ok("get", "/api/v1/catalog/products/", expect=403)

    def test_sign_in_returns_roles_and_permissions(self):
        self.staff("merch@example.com", "merchandiser")
        stores = self.ok("get", "/api/v1/auth/me/")["stores"]
        mine = next(s for s in stores if s["storeId"] == self.store_id)
        self.assertEqual(mine["role"], "staff")
        self.assertEqual(mine["roles"], ["Merchandiser"])
        self.assertIn("products.manage", mine["permissions"])
        self.assertNotIn("orders.view", mine["permissions"])

        owner = next(s for s in self.owner_me() if s["storeId"] == self.store_id)
        self.assertIn("store.manage", owner["permissions"])
        self.assertEqual(len(owner["permissions"]), 30)

    def owner_me(self):
        self.as_owner()
        return self.ok("get", "/api/v1/auth/me/")["stores"]


class TeamRulesTest(RbacTestBase):
    def test_built_in_roles_are_listed(self):
        self.assertEqual(
            {"administrator", "store_user_administrator", "online_store_editor",
             "customer_support", "merchandiser", "marketer"},
            {k for k, r in self.roles.items() if r["key"]},
        )
        admin = self.roles["administrator"]
        self.assertEqual(admin["kind"], "system")
        self.assertEqual(admin["memberCount"], 0)
        self.assertEqual(len(admin["permissions"]), 29)

    def test_system_roles_are_locked(self):
        rid = self.roles["administrator"]["id"]
        self.ok("patch", f"/api/v1/roles/{rid}/", {"name": "Boss"}, expect=403)
        self.ok("delete", f"/api/v1/roles/{rid}/", expect=403)

    def test_predefined_roles_are_editable(self):
        rid = self.roles["marketer"]["id"]
        out = self.ok("patch", f"/api/v1/roles/{rid}/", {"permissions": ["discounts.view"]})
        self.assertEqual(out["permissions"], ["discounts.view"])
        self.ok("delete", f"/api/v1/roles/{rid}/", expect=204)

    def test_custom_role_validation(self):
        self.ok("post", "/api/v1/roles/", {"name": "", "permissions": []}, expect=400)
        self.ok("post", "/api/v1/roles/", {"name": "X", "permissions": ["nope.view"]}, expect=400)
        self.ok("post", "/api/v1/roles/", {"name": "Marketer", "permissions": []}, expect=400)
        self.ok("post", "/api/v1/roles/", {"name": "Warehouse lead", "permissions": ["inventory.view"]}, expect=201)

    def test_user_admin_cannot_escalate(self):
        self.staff("ua@example.com", "store_user_administrator")
        # may hand out nothing they lack
        self.ok("post", "/api/v1/roles/", {"name": "Sneaky", "permissions": ["orders.view"]}, expect=403)
        self.ok("post", "/api/v1/roles/", {"name": "Fine", "permissions": ["members.view"]}, expect=201)

    def test_user_admin_cannot_give_administrator_or_change_owner(self):
        ua_token = self.staff("ua@example.com", "store_user_administrator")
        self.sign_up("newbie@example.com", "Newbie shop")
        self.as_owner()
        owner_membership = next(m for m in self.ok("get", "/api/v1/members/") if m["isOwner"])

        self.use(ua_token, self.store_id)
        self.ok("post", "/api/v1/members/", {
            "email": "newbie@example.com", "roleIds": [self.roles["administrator"]["id"]],
        }, expect=403)
        self.ok("delete", f"/api/v1/members/{owner_membership['membershipId']}/", expect=403)
        self.ok("patch", f"/api/v1/members/{owner_membership['membershipId']}/", {"isOwner": False}, expect=403)

    def login_as(self, email):
        r = self.client.post("/api/v1/auth/login/", {"email": email, "password": "Sturdy-pass-123"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()["access"]

    def test_last_owner_cannot_be_removed(self):
        self.as_owner()
        me = next(m for m in self.ok("get", "/api/v1/members/") if m["isOwner"])
        self.ok("delete", f"/api/v1/members/{me['membershipId']}/", expect=400)
        self.ok("patch", f"/api/v1/members/{me['membershipId']}/", {"isOwner": False}, expect=400)

    def test_owner_can_add_and_remove_owner(self):
        token, _ = self.sign_up("second@example.com", "Second")
        self.as_owner()
        m = self.ok("post", "/api/v1/members/", {"email": "second@example.com", "isOwner": True}, expect=201)
        self.assertTrue(m["isOwner"])
        self.ok("delete", f"/api/v1/members/{m['membershipId']}/", expect=204)

    def test_changing_roles_and_removing(self):
        self.staff("s@example.com", "marketer")
        self.as_owner()
        m = next(x for x in self.ok("get", "/api/v1/members/") if x["email"] == "s@example.com")
        out = self.ok("patch", f"/api/v1/members/{m['membershipId']}/", {
            "roleIds": [self.roles["merchandiser"]["id"]],
        })
        self.assertEqual([r["name"] for r in out["roles"]], ["Merchandiser"])
        self.ok("delete", f"/api/v1/members/{m['membershipId']}/", expect=204)
        self.assertEqual(len(self.ok("get", "/api/v1/members/")), 1)

    def test_unknown_account_and_bad_role_ids(self):
        self.as_owner()
        # No account yet: the access is held as an invitation until that email signs up
        invited = self.ok("post", "/api/v1/members/", {"email": "ghost@example.com", "roleIds": []}, expect=201)
        self.assertTrue(invited["invited"])
        self.sign_up("real@example.com", "Real")
        self.as_owner()
        self.ok("post", "/api/v1/members/", {"email": "real@example.com", "roleIds": ["not-a-uuid"]}, expect=400)

    def test_any_member_can_list_roles(self):
        self.staff("nobody@example.com")
        self.ok("get", "/api/v1/roles/")
        self.ok("post", "/api/v1/roles/", {"name": "X", "permissions": []}, expect=403)
        self.ok("get", "/api/v1/members/", expect=403)

    def test_only_owner_renames_store(self):
        self.staff("ua@example.com", "administrator")
        self.ok("patch", f"/api/v1/stores/{self.store_id}/", {"storeName": "Hijacked"}, expect=403)
