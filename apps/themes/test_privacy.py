from django.core import mail
from rest_framework.test import APIClient, APITestCase

from apps.core.tests.test_flow import StoreClientMixin
from apps.customers.models import Customer
from apps.tenancy.models import Store


class StorefrontPrivacyTest(StoreClientMixin, APITestCase):
    def setUp(self):
        self.token, self.store = self.sign_up("privacy@example.com", "Privacy Store")
        self.slug = self.store["storeSlug"]
        self.base = f"/s/{self.slug}"

    def configure(self, **fields):
        Store.objects.filter(store_slug=self.slug).update(**fields)

    def get(self, path, **headers):
        return APIClient().get(f"{self.base}{path}", **headers)

    def test_banner_modes(self):
        self.configure(cookie_banner="always")
        self.assertIn("tss-cookie-banner", self.get("/").content.decode())
        self.configure(cookie_banner="off")
        self.assertNotIn("tss-cookie-banner", self.get("/").content.decode())

    def test_banner_automated_follows_region(self):
        self.configure(cookie_banner="automated")
        self.assertIn("tss-cookie-banner", self.get("/", HTTP_CF_IPCOUNTRY="DE").content.decode())
        self.assertNotIn("tss-cookie-banner", self.get("/", HTTP_CF_IPCOUNTRY="IN").content.decode())
        self.assertIn("tss-cookie-banner", self.get("/").content.decode())  # unknown region: show

    def test_choice_is_remembered(self):
        self.configure(cookie_banner="always")
        client = APIClient()
        r = client.get(f"{self.base}/privacy/consent", {"choice": "accept", "next": f"{self.base}/cart"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], f"{self.base}/cart")
        self.assertNotIn("tss-cookie-banner", client.get(f"{self.base}/").content.decode())

    def test_consent_redirect_stays_inside_the_store(self):
        r = APIClient().get(f"{self.base}/privacy/consent", {"next": "https://evil.example/"})
        self.assertEqual(r["Location"], f"{self.base}/")

    def test_opt_out_page(self):
        self.configure(data_sharing_opt_out="off")
        self.assertEqual(self.get("/privacy/opt-out").status_code, 404)
        self.configure(data_sharing_opt_out="always")
        self.assertIn("tss-privacy-choices", self.get("/").content.decode())
        customer = Customer.objects.create(store_id=self.store["storeId"], email="a@example.com",
                                           email_subscription_status="not_subscribed")
        r = APIClient().post(f"{self.base}/privacy/opt-out", {"email": "a@example.com"})
        self.assertEqual(r.status_code, 200)
        customer.refresh_from_db()
        self.assertIn("data_sharing_opt_out", customer.metadata)

    def test_newsletter_double_opt_in(self):
        self.configure(double_opt_in_email=True)
        r = APIClient().post(f"{self.base}/newsletter", {"email": "n@example.com"}, format="json")
        self.assertEqual(r.status_code, 200)
        customer = Customer.objects.get(store_id=self.store["storeId"], email="n@example.com")
        self.assertNotEqual(customer.email_subscription_status, "subscribed")
        confirmations = [m for m in mail.outbox if "Confirm your subscription" in m.subject]
        self.assertEqual(len(confirmations), 1)
        token = confirmations[0].body.split("/privacy/confirm/")[1].split()[0]
        self.assertEqual(APIClient().get(f"{self.base}/privacy/confirm/{token}").status_code, 200)
        customer.refresh_from_db()
        self.assertEqual(customer.email_subscription_status, "subscribed")
        self.assertEqual(APIClient().get(f"{self.base}/privacy/confirm/bogus").status_code, 400)

    def test_newsletter_single_opt_in(self):
        self.configure(double_opt_in_email=False)
        APIClient().post(f"{self.base}/newsletter", {"email": "s@example.com"}, format="json")
        customer = Customer.objects.get(store_id=self.store["storeId"], email="s@example.com")
        self.assertEqual(customer.email_subscription_status, "subscribed")

    def test_checkout_consent_box_follows_setting(self):
        from django.test import RequestFactory
        from . import privacy

        html = '<html><body><form><button type="submit" id="checkout-submit-btn" class="x">Pay</button></form></body></html>'
        request = RequestFactory().get(f"{self.base}/checkout/")
        store = Store.objects.get(store_slug=self.slug)

        store.checkout_email_marketing, store.checkout_sms_marketing = "hidden", "hidden"
        self.assertNotIn("tss-marketing-consent", privacy.inject(html, store, request))

        store.checkout_email_marketing, store.checkout_sms_marketing = "prechecked", "unchecked"
        out = privacy.inject(html, store, request)
        self.assertIn('data-tss-consent="email_marketing" checked', out)
        self.assertIn('data-tss-consent="sms_marketing"', out)
        self.assertNotIn('data-tss-consent="sms_marketing" checked', out)
        self.assertLess(out.index("tss-marketing-consent"), out.index('id="checkout-submit-btn"'))

    def test_checkout_records_ticked_boxes_only_for_asked_channels(self):
        from django.test import RequestFactory
        from .views import _record_checkout_consent

        customer = Customer.objects.create(store_id=self.store["storeId"], email="c@example.com",
                                           email_subscription_status="not_subscribed")
        store = Store.objects.get(store_slug=self.slug)
        store.checkout_email_marketing, store.checkout_sms_marketing = "hidden", "unchecked"
        _record_checkout_consent(store, RequestFactory().post("/"), "c@example.com",
                                 {"email_marketing": True, "sms_marketing": True})
        customer.refresh_from_db()
        self.assertEqual(customer.email_subscription_status, "not_subscribed")  # email box wasn't offered
        self.assertEqual(customer.metadata.get("sms_marketing"), "subscribed")
