"""
Audit logging and the advanced role features.

    python manage.py test apps.audit --settings=config.test_settings
"""
import csv
import io

from django.core.management import call_command
from rest_framework.test import APITestCase

from apps.audit.models import AuditLog
from apps.core import rbac
from apps.core.tests.test_rbac import RbacTestBase


class AuditTestBase(RbacTestBase):
    def logs(self, query=""):
        self.as_owner()
        return self.ok("get", f"/api/v1/audit/logs/{query}")["results"]

    def make_product(self, title="Linen Shirt"):
        self.as_owner()
        return self.ok("post", "/api/v1/catalog/products/", {
            "title": title, "status": "active", "price": "1000", "sku": title[:3],
        }, expect=201)


class RecordingTest(AuditTestBase):
    def test_create_update_delete_are_logged_with_what_changed(self):
        product = self.make_product()
        pid = product["id"]
        self.ok("patch", f"/api/v1/catalog/products/{pid}/", {"title": "Wool Shirt"})
        self.ok("delete", f"/api/v1/catalog/products/{pid}/", expect=204)

        entries = [e for e in self.logs("?objectId=" + pid) if e["resource"] == "products"]
        self.assertEqual({e["action"] for e in entries}, {"products.create", "products.update", "products.delete"})

        update = next(e for e in entries if e["action"] == "products.update")
        detail = self.ok("get", f"/api/v1/audit/logs/{update['id']}/")
        change = next(c for c in detail["changes"] if c["id"] == pid)
        self.assertEqual(change["fields"]["title"], {"from": "Linen Shirt", "to": "Wool Shirt"})
        self.assertEqual(detail["actor"], "owner@example.com")
        self.assertEqual(detail["method"], "PATCH")

    def test_reads_and_failed_writes_are_not_logged(self):
        self.as_owner()
        self.ok("get", "/api/v1/catalog/products/")
        self.client.post("/api/v1/catalog/products/", {}, format="json")  # invalid
        self.assertEqual(AuditLog.objects.filter(store_id=self.store_id, resource="products").count(), 0)

    def test_secrets_are_redacted(self):
        from apps.audit import services

        self.assertEqual(services.clip("stripe_secret_key", "sk_live_x"), "[redacted]")
        self.assertEqual(services.clip("title", "x" * 400), "x" * 300 + "...")

    def test_entries_cannot_be_edited_or_deleted(self):
        self.make_product()
        entry = AuditLog.objects.filter(store_id=self.store_id).first()
        with self.assertRaises(TypeError):
            entry.save()
        with self.assertRaises(TypeError):
            entry.delete()
        with self.assertRaises(TypeError):
            AuditLog.objects.all().delete()
        with self.assertRaises(TypeError):
            AuditLog.objects.all().update(action="x")


class DeniedAndAuthTest(AuditTestBase):
    def test_a_refused_request_is_logged_as_denied(self):
        self.staff("editor@example.com", "online_store_editor")
        r = self.client.post("/api/v1/catalog/products/", {"title": "x"}, format="json")
        self.assertEqual(r.status_code, 403)

        denied = self.logs("?outcome=denied")
        self.assertEqual(len(denied), 1)
        detail = self.ok("get", f"/api/v1/audit/logs/{denied[0]['id']}/")
        self.assertEqual(detail["actor"], "editor@example.com")
        self.assertEqual(detail["metadata"]["needed"], "products.manage")

    def test_sign_in_and_failed_sign_in_are_logged(self):
        self.client.credentials()
        self.client.post("/api/v1/auth/login/", {"email": "owner@example.com", "password": "wrong"}, format="json")
        r = self.client.post("/api/v1/auth/login/", {"email": "owner@example.com", "password": "Sturdy-pass-123"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(AuditLog.objects.filter(action="auth.login_failed", actor_email="owner@example.com").exists())
        self.assertFalse(any("wrong" in str(e.metadata) + str(e.changes) for e in AuditLog.objects.all()))

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {r.json()['access']}")
        mine = self.ok("get", "/api/v1/audit/my-activity/")
        self.assertIn("auth.login", {e["action"] for e in mine})


class AccessToLogTest(AuditTestBase):
    def test_log_is_only_for_roles_with_audit_view(self):
        self.staff("support@example.com", "customer_support")
        r = self.client.get("/api/v1/audit/logs/")
        self.assertEqual(r.status_code, 403)

    def test_auditor_can_read_and_export_but_not_change(self):
        self.make_product()
        self.staff("auditor@example.com", "auditor")
        self.assertGreater(self.ok("get", "/api/v1/audit/logs/")["count"], 0)
        r = self.client.get("/api/v1/audit/logs/export/")
        self.assertEqual(r.status_code, 200)
        rows = list(csv.reader(io.StringIO(r.content.decode())))
        self.assertEqual(rows[0][0], "Time")
        self.assertEqual(self.client.post("/api/v1/catalog/products/", {"title": "x"}, format="json").status_code, 403)

    def test_export_is_itself_logged(self):
        self.as_owner()
        self.client.get("/api/v1/audit/logs/export/")
        self.assertTrue(AuditLog.objects.filter(store_id=self.store_id, action="audit.export").exists())

    def test_stores_cannot_read_each_others_log(self):
        self.make_product()
        other_token, other = self.sign_up("other@example.com", "Other")
        self.use(other_token, other["storeId"])
        self.assertEqual(self.ok("get", "/api/v1/audit/logs/")["count"], 0)

    def test_filters_and_paging(self):
        self.make_product("Aaa")
        self.make_product("Bbb")
        self.assertEqual(len(self.logs("?action=products.create")), 2)
        self.assertEqual(len(self.logs("?action=products.*")), 2)
        self.assertEqual(len(self.logs("?limit=1")), 1)
        self.assertEqual(self.ok("get", "/api/v1/audit/logs/?q=Aaa")["count"], 1)
        self.assertEqual(self.client.get("/api/v1/audit/logs/?from=nonsense").status_code, 400)
        self.assertEqual(self.client.get("/api/v1/audit/logs/?limit=abc").status_code, 400)


class RetentionTest(AuditTestBase):
    def test_prune_removes_only_old_entries(self):
        from datetime import timedelta

        from django.utils import timezone

        self.make_product()
        old = AuditLog.objects.filter(store_id=self.store_id).first()
        AuditLog.objects.filter(pk=old.pk)._raw_delete("default")  # keep one fresh entry only
        fresh = AuditLog.objects.create(store_id=self.store_id, action="x.fresh")
        stale = AuditLog.objects.create(store_id=self.store_id, action="x.stale")
        AuditLog.objects.bulk_create([])  # no-op; created_at is auto, so backdate through the DB
        from django.db import connection

        with connection.cursor() as c:
            c.execute(
                'UPDATE "audit__audit_logs" SET created_at = %s WHERE id = %s',
                [(timezone.now() - timedelta(days=400)).isoformat(sep=" "), stale.pk.hex],
            )
        call_command("prune_audit_logs", "--days", "365")
        self.assertTrue(AuditLog.objects.filter(pk=fresh.pk).exists())
        self.assertFalse(AuditLog.objects.filter(pk=stale.pk).exists())


class AdvancedRoleTest(AuditTestBase):
    def test_manage_implies_view(self):
        self.as_owner()
        role = self.ok("post", "/api/v1/roles/", {"name": "Editor", "permissions": ["products.manage"]}, expect=201)
        self.assertIn("products.view", role["permissions"])
        self.assertEqual(rbac.clean_permissions(["orders.refund"]), ["orders.refund", "orders.view"])

    def test_duplicate_role(self):
        self.as_owner()
        copy = self.ok("post", f"/api/v1/roles/{self.roles['marketer']['id']}/duplicate/", {}, expect=201)
        self.assertEqual(copy["kind"], "custom")
        self.assertEqual(copy["name"], "Marketer (copy)")
        self.assertEqual(copy["permissions"], self.roles["marketer"]["permissions"])
        again = self.ok("post", f"/api/v1/roles/{self.roles['marketer']['id']}/duplicate/", {}, expect=201)
        self.assertEqual(again["name"], "Marketer (copy 2)")

    def test_cannot_duplicate_a_role_you_do_not_fully_hold(self):
        self.staff("admin2@example.com", "store_user_administrator")
        r = self.client.post(f"/api/v1/roles/{self.roles['marketer']['id']}/duplicate/", {}, format="json")
        self.assertEqual(r.status_code, 403)

    def test_my_access(self):
        self.staff("m@example.com", "marketer")
        mine = self.ok("get", "/api/v1/roles/me/")
        self.assertFalse(mine["isOwner"])
        self.assertIn("discounts.manage", mine["permissions"])
        self.as_owner()
        self.assertTrue(self.ok("get", "/api/v1/roles/me/")["isOwner"])

    def test_role_changes_reach_the_audit_log(self):
        self.as_owner()
        self.ok("post", "/api/v1/roles/", {"name": "Custom", "permissions": ["orders.view"]}, expect=201)
        self.assertTrue(any(e["action"] == "members.post" and any(c["model"] == "StoreRole" for c in self.ok("get", f"/api/v1/audit/logs/{e['id']}/")["changes"]) for e in self.logs()))
