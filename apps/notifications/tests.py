"""
    python manage.py test apps.notifications --settings=config.test_settings
"""
from django.core import mail
from django.test import override_settings
from rest_framework.test import APITestCase

from apps.core.tests.test_flow import StoreClientMixin

from .models import NotificationSettings

SETTINGS = "/api/v1/notifications/settings/"


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, DEFAULT_FROM_EMAIL="no-reply@tss.test")
class NotificationTest(StoreClientMixin, APITestCase):
    def setUp(self):
        self.token, self.store = self.sign_up("owner@example.com", "Notify Shop")
        mail.outbox.clear()

    def place_order(self, email="buyer@example.com"):
        product = self.ok("post", "/api/v1/catalog/products/", {
            "title": "Shirt", "status": "active", "price": "100", "sku": "S-1",
        }, expect=201)
        variant = product["variants"][0]["id"]
        warehouse = self.ok("post", "/api/v1/inventory/locations/", {"name": "W"}, expect=201)
        self.ok("post", "/api/v1/inventory/items/set/", {"variant": variant, "location": warehouse["id"], "available": 5})
        customer = self.ok("post", "/api/v1/customers/", {"first_name": "Buyer", "email": email}, expect=201)
        with self.captureOnCommitCallbacks(execute=True):
            return self.ok("post", "/api/v1/orders/", {
                "items": [{"product": product["id"], "quantity": 1}], "customer": customer["id"],
            }, expect=201)

    def test_defaults_then_update(self):
        data = self.ok("get", SETTINGS)
        self.assertTrue(data["customer_order_confirmation"])
        self.assertFalse(data["staff_order_cancelled"])
        data = self.ok("patch", SETTINGS, {"sender_name": "Acme", "staff_recipients": ["A@x.com", "a@x.com", "b@x.com"]})
        self.assertEqual(data["sender_name"], "Acme")
        self.assertEqual(data["staff_recipients"], ["a@x.com", "b@x.com"])

    def test_validation(self):
        self.ok("patch", SETTINGS, {"staff_recipients": ["nope"]}, expect=400)
        self.ok("patch", SETTINGS, {"sender_name": "Evil\r\nBcc: x@y.z"}, expect=400)
        self.ok("patch", SETTINGS, {"reply_to_email": "bad"}, expect=400)

    def test_order_sends_customer_and_staff_emails(self):
        self.ok("patch", SETTINGS, {"sender_name": "Acme", "reply_to_email": "help@acme.test", "staff_recipients": ["ops@acme.test"]})
        self.place_order()
        by_to = {m.to[0]: m for m in mail.outbox}
        self.assertIn("buyer@example.com", by_to)
        self.assertIn("ops@acme.test", by_to)
        customer = by_to["buyer@example.com"]
        self.assertTrue(customer.from_email.startswith("Acme <"))
        self.assertEqual(customer.reply_to, ["help@acme.test"])

    def test_switched_off_events_send_nothing(self):
        self.ok("patch", SETTINGS, {"customer_order_confirmation": False, "staff_new_order": False})
        self.place_order()
        self.assertEqual(mail.outbox, [])

    def test_cancelled_order_emails_the_customer(self):
        order = self.place_order()
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            self.ok("post", f"/api/v1/orders/{order['id']}/cancel/")
        self.assertEqual([m.to for m in mail.outbox], [["buyer@example.com"]])

    def test_test_email(self):
        data = self.ok("post", f"{SETTINGS}test/", {"email": "me@example.com"}, expect=202)
        self.assertTrue(data["sent"])
        self.assertEqual(mail.outbox[0].to, ["me@example.com"])
        self.ok("post", f"{SETTINGS}test/", {"email": "bad"}, expect=400)

    def test_settings_are_per_store_and_need_the_permission(self):
        self.ok("patch", SETTINGS, {"sender_name": "Mine"})
        self.sign_up("other@example.com", "Other Shop")
        self.assertEqual(self.ok("get", SETTINGS)["sender_name"], "")
        self.assertEqual(NotificationSettings.objects.count(), 2)
