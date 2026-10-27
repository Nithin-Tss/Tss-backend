"""
Sign-up and the shopper's customer record. Sign-in, tokens, password reset and
email verification are covered in apps/core/tests/test_auth.py and test_security.py.

    python manage.py test apps.identity --settings=config.test_settings
"""
from django.core.cache import cache
from rest_framework.test import APITestCase

from apps.customers.models import Customer
from apps.tenancy.models import Store

SIGNUP = {
    "firstName": "Asha", "lastName": "Rao", "email": "Asha.Rao@Example.com",
    "password": "Sturdy-pass-123", "confirmPassword": "Sturdy-pass-123",
    "mobileNumber": "9876543210", "agreeTerms": True,
}


class SignupCustomerSyncTests(APITestCase):
    """Signing up from a storefront leaves the store a customer record for that shopper."""

    def setUp(self):
        cache.clear()  # sign-up is rate limited
        self.store = Store.objects.create(store_name="Shop", store_slug="shop", status="active")

    def signup(self, store=None, **changes):
        body = {**SIGNUP, **changes}
        if store is not None:
            body["storeId"] = str(store)
        return self.client.post("/api/v1/auth/signup/", body, format="json")

    def test_signup_from_a_store_creates_its_customer(self):
        r = self.signup(self.store.pk)
        self.assertEqual(r.status_code, 201, r.content)
        customer = Customer.objects.get(store=self.store)
        self.assertEqual(customer.email, "asha.rao@example.com")
        self.assertEqual((customer.first_name, customer.last_name), ("Asha", "Rao"))

    def test_store_can_be_named_by_slug_or_header(self):
        self.assertEqual(self.signup("shop").status_code, 201)
        self.assertEqual(Customer.objects.filter(store=self.store).count(), 1)

        r = self.client.post(
            "/api/v1/auth/signup/", {**SIGNUP, "email": "b@example.com"}, format="json",
            HTTP_X_STORE_ID=str(self.store.pk),
        )
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(Customer.objects.filter(store=self.store).count(), 2)

    def test_signup_does_not_subscribe_the_shopper_to_marketing_email(self):
        self.signup(self.store.pk)
        customer = Customer.objects.get(store=self.store)
        self.assertEqual(customer.email_subscription_status, "not_subscribed")

    def test_signup_does_not_invent_a_country(self):
        self.signup(self.store.pk)
        self.assertFalse(Customer.objects.get(store=self.store).country)

    def test_signup_without_a_store_creates_no_customer(self):
        self.assertEqual(self.signup().status_code, 201)
        self.assertEqual(Customer.objects.count(), 0)

    def test_unknown_store_is_ignored_and_the_account_is_still_made(self):
        for ref in ("not-a-store", "00000000-0000-0000-0000-000000000000"):
            cache.clear()
            r = self.signup(ref, email=f"{ref[:4]}@example.com")
            self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(Customer.objects.count(), 0)

    def test_closed_and_inactive_stores_get_no_customer(self):
        for n, status in enumerate(("closed", "draft", "store_closed")):
            cache.clear()
            store = Store.objects.create(store_name=f"S{n}", store_slug=f"s{n}", status=status)
            r = self.signup(store.pk, email=f"s{n}@example.com")
            self.assertEqual(r.status_code, 201, r.content)
            self.assertFalse(Customer.objects.filter(store=store).exists(), status)

    def test_existing_guest_customer_is_reused_not_duplicated(self):
        guest = Customer.objects.create(
            store=self.store, email="asha.rao@example.com", email_subscription_status="not_subscribed"
        )
        self.signup(self.store.pk)
        self.assertEqual(Customer.objects.filter(store=self.store).count(), 1)
        guest.refresh_from_db()
        self.assertEqual(guest.first_name, "Asha")

    def test_existing_customer_names_are_never_overwritten(self):
        Customer.objects.create(
            store=self.store, email="asha.rao@example.com", first_name="Ash", last_name="R",
            email_subscription_status="subscribed",
        )
        self.signup(self.store.pk)
        customer = Customer.objects.get(store=self.store)
        self.assertEqual((customer.first_name, customer.last_name), ("Ash", "R"))
        self.assertEqual(customer.email_subscription_status, "subscribed")

    def test_the_same_email_is_a_separate_customer_in_each_store(self):
        other = Store.objects.create(store_name="Other", store_slug="other", status="active")
        self.signup(self.store.pk)
        # Same person signs up again for the second store: the account exists, so it is refused,
        # and nothing leaks into the second store.
        cache.clear()
        self.assertEqual(self.signup(other.pk).status_code, 400)
        self.assertFalse(Customer.objects.filter(store=other).exists())
