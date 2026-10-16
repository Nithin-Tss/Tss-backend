"""
Authentication and security tests that go beyond sign-in (test_auth.py) and roles (test_rbac.py):

- password reset and email verification links
- store scoping: the X-Store-Id header, and one store never reaching another's data
- an audit of every API route: who may call it, and what a stranger gets back
- shoppers on the public storefront (X-Cart-Session) cannot reach each other's carts or orders
- hardening: mass assignment, hostile input, CORS, security headers, production settings

    python manage.py test apps.core.tests.test_security --settings=config.test_settings
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core import mail, signing
from django.core.cache import cache
from django.test import override_settings
from django.urls import get_resolver, resolve
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework.test import APIClient, APITestCase

from apps.catalog.models import Product
from apps.commerce.tests import CommerceTestBase
from apps.customers.models import Customer
from apps.identity.email_links import VERIFY_EMAIL_SALT
from apps.identity.models import User
from apps.orders.models import Order

from .test_flow import StoreClientMixin

PASSWORD = "Sturdy-pass-123"
NEW_PASSWORD = "Fresh-Pass-456!"
ZERO_UUID = "00000000-0000-0000-0000-000000000000"

SIGNUP = {
    "firstName": "Ravi", "lastName": "Kumar", "email": "ravi@example.com",
    "password": PASSWORD, "confirmPassword": PASSWORD,
    "mobileNumber": "9876543210", "agreeTerms": True,
}


def link_params(message):
    """The query parameters of the (only) link in an email body."""
    url = re.search(r"https?://\S+", message.body).group(0)
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def link_uid_raw(value):
    return urlsafe_base64_encode(force_bytes(value))


def link_uid(user):
    return link_uid_raw(user.pk)


class AccountTestBase(APITestCase):
    def setUp(self):
        cache.clear()  # rate limits are per client address: start every test fresh
        mail.outbox.clear()

    def signup(self, **changes):
        return self.client.post("/api/v1/auth/signup/", {**SIGNUP, **changes}, format="json")

    def login(self, email="ravi@example.com", password=PASSWORD):
        return self.client.post("/api/v1/auth/login/", {"email": email, "password": password}, format="json")

    def me(self, token):
        return self.client.get("/api/v1/auth/me/", HTTP_AUTHORIZATION=f"Bearer {token}")

    def post(self, url, body, token=None):
        extra = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return self.client.post(url, body, format="json", **extra)


# ======================================================================= password reset

class PasswordResetTest(AccountTestBase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(
            email="ravi@example.com", password=PASSWORD, first_name="Ravi", last_name="Kumar",
        )

    def request_link(self, email="ravi@example.com"):
        r = self.post("/api/v1/auth/password-reset/", {"email": email})
        self.assertEqual(r.status_code, 200, r.content)
        return link_params(mail.outbox[-1])

    def confirm(self, params, **body):
        return self.post("/api/v1/auth/password-reset/confirm/", {
            **params, "newPassword": NEW_PASSWORD, "confirmPassword": NEW_PASSWORD, **body,
        })

    def test_request_gives_the_same_answer_whether_or_not_the_account_exists(self):
        known = self.post("/api/v1/auth/password-reset/", {"email": "ravi@example.com"})
        unknown = self.post("/api/v1/auth/password-reset/", {"email": "nobody@example.com"})

        self.assertEqual(known.status_code, 200)
        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(known.json(), unknown.json())  # cannot be used to find out who has an account

        self.assertEqual(len(mail.outbox), 1)  # and only the real account got an email
        self.assertEqual(mail.outbox[0].to, ["ravi@example.com"])

    def test_request_ignores_letter_case_and_rejects_junk(self):
        self.post("/api/v1/auth/password-reset/", {"email": "RAVI@Example.COM"})
        self.assertEqual(len(mail.outbox), 1)

        self.assertEqual(self.post("/api/v1/auth/password-reset/", {"email": "not-an-email"}).status_code, 400)
        self.assertEqual(self.post("/api/v1/auth/password-reset/", {}).status_code, 400)

    def test_the_email_carries_a_link_but_never_the_password(self):
        self.request_link()
        body = mail.outbox[0].body
        self.assertIn(settings.FRONTEND_URL.rstrip("/") + "/auth/reset-password?", body)
        self.assertNotIn(PASSWORD, body)

    def test_link_sets_the_new_password_once(self):
        params = self.request_link()

        r = self.post("/api/v1/auth/password-reset/validate/", params)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"valid": True, "email": "ravi@example.com"})

        r = self.confirm(params)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertNotIn("access", r.json())  # resetting does not sign anyone in

        self.assertEqual(self.login(password=NEW_PASSWORD).status_code, 200)
        self.assertEqual(self.login(password=PASSWORD).status_code, 400)

        again = self.confirm(params, newPassword="Another-Pass-789!", confirmPassword="Another-Pass-789!")
        self.assertIn("token", again.json())  # single use
        self.assertEqual(again.status_code, 400)
        self.assertEqual(self.post("/api/v1/auth/password-reset/validate/", params).status_code, 400)
        self.assertEqual(self.login(password=NEW_PASSWORD).status_code, 200)  # unchanged by the replay

    def test_reset_signs_out_every_device(self):
        access = self.login().json()["access"]
        refresh = self.login().json()["refresh"]
        browser = APIClient()
        browser.force_login(self.user)
        self.assertEqual(browser.get("/api/v1/auth/me/").status_code, 200)

        self.confirm(self.request_link())

        self.assertEqual(self.me(access).status_code, 401)
        self.assertEqual(self.post("/api/v1/auth/refresh/", {"refresh": refresh}).status_code, 401)
        self.assertEqual(browser.get("/api/v1/auth/me/").status_code, 401)

    def test_opening_the_link_proves_the_email_address(self):
        self.assertIsNone(self.user.email_verified_at)
        self.confirm(self.request_link())
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.email_verified_at)

    def test_weak_or_mismatched_password_is_refused_and_the_link_survives(self):
        params = self.request_link()

        weak = self.confirm(params, newPassword="12345678", confirmPassword="12345678")
        self.assertEqual(weak.status_code, 400)
        self.assertIn("newPassword", weak.json())

        no_mix = self.confirm(params, newPassword="alllowercase", confirmPassword="alllowercase")
        self.assertIn("newPassword", no_mix.json())

        mismatch = self.confirm(params, confirmPassword="Different-123!")
        self.assertIn("confirmPassword", mismatch.json())

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))  # still the old password
        self.assertEqual(self.confirm(params).status_code, 200)  # and the link still works

    def test_forged_tampered_and_borrowed_links_are_refused(self):
        params = self.request_link()
        other = User.objects.create_user(email="other@example.com", password=PASSWORD,
                                         first_name="Other", last_name="User")
        other_params = {"uid": link_uid(other), "token": params["token"]}  # Ravi's token, someone else's uid

        bad = [
            {**params, "token": params["token"][:-2] + "xx"},     # tampered
            {**params, "token": "not-a-token"},
            {**params, "uid": "!!!"},                              # not base64
            {**params, "uid": link_uid_raw("not-a-uuid")},         # not a user id
            {**params, "uid": link_uid_raw(ZERO_UUID)},            # no such user
            other_params,
            {"uid": params["uid"]},                                # missing token
            {"token": params["token"]},                            # missing uid
        ]
        for body in bad:
            for url in ("validate/", "confirm/"):
                cache.clear()  # these endpoints are rate limited too; this test is about the links
                r = self.post(f"/api/v1/auth/password-reset/{url}", {
                    **body, "newPassword": NEW_PASSWORD, "confirmPassword": NEW_PASSWORD,
                })
                self.assertEqual(r.status_code, 400, (url, body, r.content))

        self.assertTrue(User.objects.get(email="other@example.com").check_password(PASSWORD))
        self.assertTrue(User.objects.get(email="ravi@example.com").check_password(PASSWORD))

    def test_link_expires(self):
        params = self.request_link()
        later = datetime.now() + timedelta(seconds=settings.PASSWORD_RESET_TIMEOUT + 60)
        with mock.patch.object(default_token_generator, "_now", return_value=later):
            self.assertEqual(self.post("/api/v1/auth/password-reset/validate/", params).status_code, 400)
            self.assertEqual(self.confirm(params).status_code, 400)
        self.assertEqual(self.confirm(params).status_code, 200)  # still fine inside its hour

    def test_signing_in_retires_an_unused_reset_link(self):
        # Django's reset token includes the last sign-in time: remembering the password
        # and signing in is the cue that the emailed link is no longer wanted.
        params = self.request_link()
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(self.confirm(params).status_code, 400)

    def test_a_verification_token_is_not_a_reset_token(self):
        verification = signing.dumps({"user": str(self.user.pk), "email": self.user.email},
                                     salt=VERIFY_EMAIL_SALT)
        r = self.confirm({"uid": link_uid(self.user), "token": verification})
        self.assertEqual(r.status_code, 400)

    def test_requests_are_rate_limited_to_stop_mailbox_flooding(self):
        codes = [self.post("/api/v1/auth/password-reset/", {"email": "ravi@example.com"}).status_code
                 for _ in range(7)]
        self.assertEqual(codes[:5], [200] * 5)
        self.assertIn(429, codes)
        self.assertLessEqual(len(mail.outbox), 5)


# ================================================================== email verification

class EmailVerificationTest(AccountTestBase):
    def setUp(self):
        super().setUp()
        r = self.signup()
        self.assertEqual(r.status_code, 201, r.content)
        self.tokens = r.json()
        self.token = link_params(mail.outbox[0])["token"]
        self.client.cookies.clear()  # sign-up also left a session cookie: these tests use tokens only

    def verify(self, token):
        return self.post("/api/v1/auth/verify-email/", {"token": token})

    def verified(self):
        return self.me(self.tokens["access"]).json()["user"]["emailVerified"]

    def test_sign_up_sends_one_link_to_the_new_address(self):
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["ravi@example.com"])
        self.assertFalse(self.tokens["user"]["emailVerified"])

    def test_link_verifies_the_address_even_from_another_device(self):
        self.assertFalse(self.verified())
        r = self.verify(self.token)  # no sign-in on this request
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["emailVerified"], True)
        self.assertTrue(self.verified())
        self.assertEqual(self.verify(self.token).status_code, 200)  # opening it twice is harmless

    def test_bad_links_are_refused(self):
        user = User.objects.get(email="ravi@example.com")
        wrong_purpose = signing.dumps({"user": str(user.pk), "email": user.email}, salt="something.else")
        reset_token = default_token_generator.make_token(user)

        for token in (self.token[:-2] + "xx", "garbage", "", wrong_purpose, reset_token):
            self.assertEqual(self.verify(token).status_code, 400, token)
        self.assertEqual(self.post("/api/v1/auth/verify-email/", {}).status_code, 400)
        self.assertFalse(self.verified())

    def test_link_expires(self):
        with self.settings(EMAIL_VERIFICATION_TIMEOUT=-1):
            self.assertEqual(self.verify(self.token).status_code, 400)
        self.assertFalse(self.verified())

    def test_changing_the_email_needs_a_new_verification(self):
        self.verify(self.token)
        self.assertTrue(self.verified())

        r = self.client.patch("/api/v1/auth/me/", {"email": "ravi.new@example.com"}, format="json",
                              HTTP_AUTHORIZATION=f"Bearer {self.tokens['access']}")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(r.json()["user"]["emailVerified"])

        self.assertEqual(self.verify(self.token).status_code, 400)  # the old link is for the old address
        self.assertFalse(self.verified())

        self.assertEqual(mail.outbox[-1].to, ["ravi.new@example.com"])
        self.assertEqual(self.verify(link_params(mail.outbox[-1])["token"]).status_code, 200)
        self.assertTrue(self.verified())

    def test_resend_needs_sign_in(self):
        r = self.post("/api/v1/auth/verify-email/resend/", {})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(len(mail.outbox), 1)

    def test_resend_sends_only_while_unverified(self):
        r = self.post("/api/v1/auth/verify-email/resend/", {}, token=self.tokens["access"])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["emailVerified"], False)
        self.assertEqual(len(mail.outbox), 2)

        self.verify(self.token)
        r = self.post("/api/v1/auth/verify-email/resend/", {}, token=self.tokens["access"])
        self.assertEqual(r.json()["emailVerified"], True)
        self.assertEqual(len(mail.outbox), 2)  # nothing more to send

    def test_resend_is_rate_limited(self):
        codes = [self.post("/api/v1/auth/verify-email/resend/", {}, token=self.tokens["access"]).status_code
                 for _ in range(7)]
        self.assertIn(429, codes)


# ===================================================================== store scoping

class StoreHeaderTest(StoreClientMixin, APITestCase):
    """X-Store-Id picks the store; it must never grant more than the user's membership does."""

    def setUp(self):
        self.token_a, self.store_a = self.sign_up("a@example.com", "Store A")
        self.product_a = self.ok("post", "/api/v1/catalog/products/",
                                 {"title": "A-only", "sku": "A-1", "price": "10"}, expect=201)
        self.customer_a = self.ok("post", "/api/v1/customers/", {"first_name": "Alice"}, expect=201)
        self.token_b, self.store_b = self.sign_up("b@example.com", "Store B")

    def get(self, url, **headers):
        self.client.credentials(**headers)
        return self.client.get(url)

    def test_header_is_required_and_must_be_a_store_id(self):
        bearer = {"HTTP_AUTHORIZATION": f"Bearer {self.token_b}"}
        self.assertEqual(self.get("/api/v1/orders/", **bearer).status_code, 400)
        self.assertEqual(self.get("/api/v1/orders/", **bearer, HTTP_X_STORE_ID="nope").status_code, 400)
        self.assertEqual(self.get("/api/v1/orders/", **bearer, HTTP_X_STORE_ID="1; DROP TABLE x").status_code, 400)

    def test_someone_elses_store_and_a_missing_store_look_identical(self):
        bearer = {"HTTP_AUTHORIZATION": f"Bearer {self.token_b}"}
        foreign = self.get("/api/v1/orders/", **bearer, HTTP_X_STORE_ID=self.store_a["storeId"])
        missing = self.get("/api/v1/orders/", **bearer, HTTP_X_STORE_ID=ZERO_UUID)
        self.assertEqual(foreign.status_code, 403)
        self.assertEqual(missing.status_code, 403)
        self.assertEqual(foreign.json(), missing.json())  # store ids cannot be probed

    def test_the_header_needs_a_sign_in_too(self):
        self.client.cookies.clear()  # sign-up left a session cookie behind
        r = self.get("/api/v1/orders/", HTTP_X_STORE_ID=self.store_a["storeId"])
        self.assertEqual(r.status_code, 401)

    def test_borrowing_a_store_id_cannot_write_into_that_store(self):
        self.use(self.token_b, self.store_a["storeId"])
        for url, body in (
            ("/api/v1/catalog/products/", {"title": "Planted", "sku": "X-1", "price": "1"}),
            ("/api/v1/customers/", {"first_name": "Planted"}),
            ("/api/v1/inventory/locations/", {"name": "Planted"}),
        ):
            self.ok("post", url, body, expect=403)
        self.assertFalse(Product.objects.filter(title="Planted").exists())
        self.assertFalse(Customer.objects.filter(first_name="Planted").exists())

    def test_other_stores_records_cannot_be_changed_or_deleted_by_id(self):
        self.use(self.token_b, self.store_b["storeId"])  # B is in its own store, guessing A's ids
        pid, cid = self.product_a["id"], self.customer_a["id"]

        self.ok("patch", f"/api/v1/catalog/products/{pid}/", {"title": "Hijacked"}, expect=404)
        self.ok("delete", f"/api/v1/catalog/products/{pid}/", expect=404)
        self.ok("patch", f"/api/v1/customers/{cid}/", {"first_name": "Hijacked"}, expect=404)
        self.ok("delete", f"/api/v1/customers/{cid}/", expect=404)

        self.assertEqual(Product.objects.get(pk=pid).title, "A-only")
        self.assertEqual(Customer.objects.get(pk=cid).first_name, "Alice")

    def test_stores_list_and_detail_show_only_your_own(self):
        self.use(self.token_b, self.store_b["storeId"])
        listed = self.ok("get", "/api/v1/stores/")
        self.assertEqual([s["storeId"] for s in listed], [self.store_b["storeId"]])

        sid = self.store_a["storeId"]
        self.ok("get", f"/api/v1/stores/{sid}/", expect=404)
        self.ok("patch", f"/api/v1/stores/{sid}/", {"storeName": "Hijacked"}, expect=404)
        self.ok("delete", f"/api/v1/stores/{sid}/", expect=404)
        self.ok("post", f"/api/v1/stores/{sid}/reopen/", {}, expect=403)  # not an administrator

    def test_only_a_platform_admin_can_reopen_a_store(self):
        sid = self.store_b["storeId"]
        self.ok("delete", f"/api/v1/stores/{sid}/", expect=204)  # B's owner closes it
        self.ok("post", f"/api/v1/stores/{sid}/reopen/", {}, expect=403)  # ...but cannot undo it alone


# ========================================================================= route audit

def api_routes():
    """(route, view class or None, url that resolves to it, callback name) for every /api/v1/ route."""
    found = []

    def walk(patterns, prefix=""):
        for p in patterns:
            route = prefix + str(p.pattern)
            if hasattr(p, "url_patterns"):
                walk(p.url_patterns, route)
                continue
            if not route.startswith("api/v1/"):
                continue
            url = re.sub(r"\(\?P<\w+>[^)]*\)", ZERO_UUID, route)  # router regex groups
            url = re.sub(r"<uuid:\w+>", ZERO_UUID, url)
            url = re.sub(r"<(?:slug|str):\w+>", "x", url)
            url = re.sub(r"<int:\w+>", "1", url)
            url = "/" + url.replace("^", "").replace("$", "")
            found.append((route, getattr(p.callback, "cls", None), url, p.callback.__name__))

    walk(get_resolver().url_patterns)
    return found


class RouteAuditTest(StoreClientMixin, APITestCase):
    """
    Every API route is classified. A new public route, or a new route that skips the store
    checks, makes these tests fail until someone decides it is meant to be that way.
    """

    # No sign-in needed (rate-limited sign-up/in, reset links, and the shoppers' storefront API)
    PUBLIC = {
        "SignupView", "LoginView", "RefreshView", "LogoutView",
        "PasswordResetRequestView", "PasswordResetValidateView", "PasswordResetConfirmView", "VerifyEmailView",
        "CartView", "CartItemsView", "CartItemView", "CheckoutStartView", "CheckoutDetailView",
        "CheckoutCompleteView", "OrderListView", "OrderDetailView", "OrderCancelView",
    }
    # Sign-in only: they are about the account itself, or do their own store resolution
    ACCOUNT_ONLY = {
        "MeView", "ChangePasswordView", "ResendVerificationEmailView",
        "StoreViewSet", "CustomerAddressViewSet", "ThemeSchemaView",
    }
    # Plain function views, outside DRF
    FUNCTION_VIEWS = {"theme_css"}
    METHODS = ("get", "post", "put", "patch", "delete")
    STORE_PERMISSIONS = ["IsAuthenticated", "IsStoreMember", "HasStorePermission"]

    def classified(self):
        public, account_only, store_scoped, other = {}, {}, {}, {}
        for route, cls, url, name in api_routes():
            if cls is None:
                other[route] = name
                continue
            perms = [c.__name__ for c in cls.permission_classes]
            if not perms or "AllowAny" in perms:
                public[route] = (cls, url)
            elif perms == ["IsAuthenticated"]:
                account_only[route] = (cls, url)
            elif perms == self.STORE_PERMISSIONS:
                store_scoped[route] = (cls, url)
            else:
                self.fail(f"{route}: unexpected permission classes {perms}")
        return public, account_only, store_scoped, other

    def test_the_audit_sees_the_whole_api(self):
        public, account_only, store_scoped, other = self.classified()
        self.assertGreater(len(store_scoped), 80)
        for route, cls, url, _ in api_routes():
            if cls is not None:  # the url made from the route really reaches that view
                self.assertIs(resolve(url).func.cls, cls, (route, url))

    def test_only_the_expected_routes_are_public(self):
        public, *_ = self.classified()
        self.assertEqual({cls.__name__ for cls, _ in public.values()}, self.PUBLIC)

    def test_only_the_expected_routes_skip_the_store_checks(self):
        _, account_only, _, other = self.classified()
        self.assertEqual({cls.__name__ for cls, _ in account_only.values()}, self.ACCOUNT_ONLY)
        self.assertEqual(set(other.values()), self.FUNCTION_VIEWS)

    def test_signed_in_routes_accept_only_tokens_and_sessions(self):
        _, account_only, store_scoped, _ = self.classified()
        for route, (cls, _) in {**account_only, **store_scoped}.items():
            self.assertEqual(
                [c.__name__ for c in cls.authentication_classes],
                ["JWTAuthentication", "SessionAuthentication"], route,
            )

    def test_strangers_get_401_from_every_protected_route_and_method(self):
        _, account_only, store_scoped, _ = self.classified()
        self.client.credentials()
        self.client.cookies.clear()
        for route, (cls, url) in {**account_only, **store_scoped}.items():
            for method in self.METHODS:
                r = getattr(self.client, method)(url, {}, format="json")
                self.assertEqual(r.status_code, 401, f"{method.upper()} {url} ({cls.__name__})")

    def test_a_forged_or_garbage_token_is_no_better_than_none(self):
        _, _, store_scoped, _ = self.classified()
        for route, (cls, url) in list(store_scoped.items())[::10]:
            for token in ("garbage", "a.b.c"):
                r = self.client.get(url, HTTP_AUTHORIZATION=f"Bearer {token}", HTTP_X_STORE_ID=ZERO_UUID)
                self.assertEqual(r.status_code, 401, url)

    def test_store_routes_need_the_store_header(self):
        token, _ = self.sign_up("lonely@example.com", "Lonely")
        _, _, store_scoped, _ = self.classified()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")  # token, no X-Store-Id
        for route, (cls, url) in store_scoped.items():
            for method in self.METHODS:
                r = getattr(self.client, method)(url, {}, format="json")
                self.assertEqual(r.status_code, 400, f"{method.upper()} {url} ({cls.__name__})")

    def test_no_store_route_can_be_reached_through_a_store_you_do_not_belong_to(self):
        _, store_a = self.sign_up("a@example.com", "Store A")
        token_b, _ = self.sign_up("b@example.com", "Store B")
        _, _, store_scoped, _ = self.classified()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token_b}", HTTP_X_STORE_ID=store_a["storeId"])
        for route, (cls, url) in store_scoped.items():
            for method in self.METHODS:
                r = getattr(self.client, method)(url, {}, format="json")
                self.assertEqual(r.status_code, 403, f"{method.upper()} {url} ({cls.__name__})")


# ================================================================ storefront shoppers

class StorefrontSessionSecurityTest(CommerceTestBase):
    """Shoppers have no account: the X-Cart-Session they send is all that separates them."""

    base = "/api/v1/storefront/shop"
    address = {"name": "A", "line1": "1 Main St", "city": "Chennai", "postal_code": "600001", "country": "IN"}

    def as_session(self, session, method, path, body=None):
        return getattr(self.client, method)(
            f"{self.base}{path}", body, format="json", HTTP_X_CART_SESSION=session,
        )

    def place_order(self, session):
        r = self.as_session(session, "post", "/cart/items/", {"product": str(self.product.pk), "quantity": 1})
        self.assertEqual(r.status_code, 201, r.content)
        r = self.as_session(session, "post", "/checkout/", {"email": "a@b.com", "shipping_address": self.address})
        checkout_id = r.json()["id"]
        r = self.as_session(session, "post", f"/checkout/{checkout_id}/complete/")
        self.assertEqual(r.status_code, 201, r.content)
        return checkout_id, r.json()["id"]

    def test_another_session_cannot_read_change_or_cancel_my_order(self):
        checkout_id, order_id = self.place_order("alice-session")

        self.assertEqual([o["id"] for o in self.as_session("alice-session", "get", "/orders/").json()], [order_id])
        self.assertEqual(self.as_session("mallory-session", "get", "/orders/").json(), [])
        self.assertEqual(self.as_session("mallory-session", "get", f"/orders/{order_id}/").status_code, 404)
        self.assertEqual(self.as_session("mallory-session", "post", f"/orders/{order_id}/cancel/").status_code, 404)
        self.assertNotEqual(Order.objects.get(pk=order_id).status, "cancelled")

        for method, path in (("get", f"/checkout/{checkout_id}/"), ("patch", f"/checkout/{checkout_id}/"),
                             ("post", f"/checkout/{checkout_id}/complete/")):
            self.assertEqual(self.as_session("mallory-session", method, path, {}).status_code, 404, path)

    def test_the_same_session_id_in_another_store_sees_nothing(self):
        from apps.tenancy.models import Store
        Store.objects.create(store_name="Other", store_slug="other", status="active")
        checkout_id, order_id = self.place_order("alice-session")

        theirs = "/api/v1/storefront/other"
        h = {"HTTP_X_CART_SESSION": "alice-session"}
        self.assertEqual(self.client.get(f"{theirs}/orders/", **h).json(), [])
        self.assertEqual(self.client.get(f"{theirs}/orders/{order_id}/", **h).status_code, 404)
        self.assertEqual(self.client.get(f"{theirs}/checkout/{checkout_id}/", **h).status_code, 404)

    def test_every_storefront_route_needs_the_session_header(self):
        for path in ("/cart/", "/orders/"):
            self.assertEqual(self.client.get(f"{self.base}{path}").status_code, 400, path)
        self.assertEqual(self.client.get(f"{self.base}/cart/", HTTP_X_CART_SESSION="").status_code, 400)

    def test_closed_stores_have_no_storefront_api(self):
        from apps.tenancy.models import Store
        Store.objects.filter(pk=self.store.pk).update(status="closed")
        self.assertEqual(self.as_session("s", "get", "/cart/").status_code, 404)

    def test_carts_reject_other_stores_products_and_nonsense_quantities(self):
        from apps.tenancy.models import Store
        other = Store.objects.create(store_name="Other", store_slug="other", status="active")
        foreign = Product.objects.create(store=other, title="Foreign", status="active")

        r = self.as_session("s", "post", "/cart/items/", {"product": str(foreign.pk), "quantity": 1})
        self.assertEqual(r.status_code, 400)

        for quantity in (0, -1, "lots", None, 1.5):
            r = self.as_session("s", "post", "/cart/items/", {"product": str(self.product.pk), "quantity": quantity})
            self.assertEqual(r.status_code, 400, quantity)

        item = self.as_session("s", "post", "/cart/items/", {"product": str(self.product.pk), "quantity": 1}).json()
        item_id = item["items"][0]["id"]
        self.assertEqual(self.as_session("s", "patch", f"/cart/items/{item_id}/", {"quantity": -5}).status_code, 400)

    def test_shoppers_have_no_way_into_the_staff_api(self):
        for path in ("/api/v1/carts/", "/api/v1/checkouts/"):
            r = self.client.get(path, HTTP_X_CART_SESSION="alice-session", HTTP_X_STORE_ID=str(self.store.pk))
            self.assertIn(r.status_code, (401, 403), path)


# ===================================================================== hardening

class HardeningTest(AccountTestBase):
    def test_accounts_cannot_make_themselves_privileged(self):
        r = self.signup(is_owner=True, is_staff=True, is_superuser=True, is_active=True,
                        email_verified_at="2020-01-01T00:00:00Z", id=ZERO_UUID, password_hash="x")
        self.assertEqual(r.status_code, 201, r.content)
        user = User.objects.get(email="ravi@example.com")
        self.assertFalse(user.is_owner or user.is_staff or user.is_superuser)
        self.assertIsNone(user.email_verified_at)
        self.assertNotEqual(str(user.pk), ZERO_UUID)

        token = r.json()["access"]
        r = self.client.patch(
            "/api/v1/auth/me/", {"firstName": "Ravi", "is_owner": True, "emailVerified": True,
                                 "email_verified_at": "2020-01-01T00:00:00Z"},
            format="json", HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(r.status_code, 200, r.content)
        user.refresh_from_db()
        self.assertFalse(user.is_owner)
        self.assertIsNone(user.email_verified_at)
        self.assertEqual(self.client.get("/admin/").status_code, 302)  # still not an administrator

    def test_responses_never_contain_password_data(self):
        r = self.signup()
        text = r.content.decode()
        for needle in (PASSWORD, "pbkdf2", "argon2", "md5$", "password_hash", "\"password\""):
            self.assertNotIn(needle, text)
        self.assertEqual(set(r.json()["user"]), {"id", "email", "firstName", "lastName", "mobileNumber", "emailVerified"})

        me = self.client.get("/api/v1/auth/me/", HTTP_AUTHORIZATION=f"Bearer {r.json()['access']}")
        self.assertNotIn("password", me.content.decode().lower())

    def test_names_and_numbers_are_validated_so_markup_is_refused(self):
        for changes, field in (
            ({"firstName": "<script>alert(1)</script>"}, "firstName"),
            ({"lastName": "Kumar<img src=x onerror=alert(1)>"}, "lastName"),
            ({"mobileNumber": "98765"}, "mobileNumber"),
            ({"mobileNumber": "98765abcde"}, "mobileNumber"),
            ({"email": "a@b.com<script>"}, "email"),
        ):
            r = self.signup(**changes)
            self.assertEqual(r.status_code, 400, changes)
            self.assertIn(field, r.json())
        self.assertEqual(User.objects.count(), 0)

    def test_hostile_input_is_answered_with_400_never_a_crash_or_a_sign_in(self):
        self.signup()
        cache.clear()
        hostile = [
            {"email": "' OR '1'='1' --", "password": "' OR '1'='1' --"},
            {"email": "ravi@example.com' --", "password": PASSWORD},
            {"email": ["ravi@example.com"], "password": PASSWORD},
            {"email": {"$ne": ""}, "password": {"$ne": ""}},
            {"email": None, "password": None},
            {"email": "ravi@example.com", "password": "x" * 10_000},
            {"email": "r" * 400 + "@example.com", "password": PASSWORD},
            {"email": "ravi@example.com\r\nBcc: evil@example.com", "password": PASSWORD},
            {},
        ]
        for body in hostile:
            r = self.client.post("/api/v1/auth/login/", body, format="json")
            self.assertEqual(r.status_code, 400, body)
            self.assertNotIn("access", r.json())
            cache.clear()

        for raw in ("not json at all", "[1, 2, 3]", '"just a string"', "null"):
            r = self.client.post("/api/v1/auth/login/", raw, content_type="application/json")
            self.assertEqual(r.status_code, 400, raw)
            cache.clear()

    def test_security_headers_are_on_every_response(self):
        for url in ("/api/v1/auth/me/", "/api/v1/auth/login/", "/admin/login/", "/api/v1/nope/"):
            r = self.client.get(url)
            self.assertEqual(r["X-Content-Type-Options"], "nosniff", url)
            self.assertEqual(r["X-Frame-Options"], "DENY", url)
            self.assertEqual(r["Referrer-Policy"], "same-origin", url)

    def test_cors_only_answers_the_configured_frontends(self):
        allowed, evil = "https://shop.example.com", "https://evil.example.net"

        def preflight(origin):
            return self.client.options(
                "/api/v1/auth/login/", HTTP_ORIGIN=origin,
                HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
                HTTP_ACCESS_CONTROL_REQUEST_HEADERS="authorization,content-type,x-store-id",
            )

        with override_settings(CORS_ALLOWED_ORIGINS=[allowed]):
            ok, bad = preflight(allowed), preflight(evil)
            self.assertEqual(ok["Access-Control-Allow-Origin"], allowed)
            self.assertNotIn("Access-Control-Allow-Origin", bad)
            self.assertNotEqual(ok["Access-Control-Allow-Origin"], "*")
            self.assertNotIn("Access-Control-Allow-Credentials", ok)  # cookies are not shared cross-site

            actual = self.client.post("/api/v1/auth/login/", {"email": "x@y.com", "password": "x"},
                                      format="json", HTTP_ORIGIN=evil)
            self.assertNotIn("Access-Control-Allow-Origin", actual)

    def test_the_session_cookie_is_http_only_and_same_site(self):
        cookie = self.signup().cookies["sessionid"]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")

    def test_admin_site_is_closed_to_everyone_but_platform_admins(self):
        self.signup()
        self.assertEqual(self.client.get("/admin/").status_code, 302)
        self.assertEqual(self.client.get("/admin/identity/user/").status_code, 302)
        self.assertEqual(self.client.post("/admin/identity/user/add/", {"email": "x@y.com"}).status_code, 302)


class InjectionTest(StoreClientMixin, APITestCase):
    def test_search_filters_are_safe_against_injection(self):
        self.sign_up("inj@example.com", "Inj")
        for term in ("'; DROP TABLE products; --", "\" OR 1=1 --", "%", "\\", "<script>", "\x00"):
            for url in ("/api/v1/catalog/products/", "/api/v1/customers/", "/api/v1/orders/"):
                r = self.client.get(url, {"search": term, "status": term, "ordering": term})
                self.assertLess(r.status_code, 500, (url, term))
        self.assertEqual(self.client.get("/api/v1/catalog/products/").status_code, 200)


# ================================================================ production settings

class ProductionSettingsTest(APITestCase):
    """
    Loads config.settings in a fresh interpreter with DEBUG off, the way a server would,
    ignoring this machine's .env.
    """

    PROBE = """
import json, os, sys
sys.path.insert(0, {base!r})
import dotenv
dotenv.load_dotenv = lambda *a, **k: False
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
import django
django.setup()
from django.conf import settings
from django.core.checks import run_checks
print(json.dumps({{
    "DEBUG": settings.DEBUG,
    "ssl_redirect": settings.SECURE_SSL_REDIRECT,
    "hsts": settings.SECURE_HSTS_SECONDS,
    "session_secure": settings.SESSION_COOKIE_SECURE,
    "csrf_secure": settings.CSRF_COOKIE_SECURE,
    "proxy": settings.SECURE_PROXY_SSL_HEADER,
    "hosts": settings.ALLOWED_HOSTS,
    "hasher": settings.PASSWORD_HASHERS[0],
    "validators": [v["NAME"].rsplit(".", 1)[-1] for v in settings.AUTH_PASSWORD_VALIDATORS],
    "access_minutes": settings.SIMPLE_JWT["ACCESS_TOKEN_LIFETIME"].total_seconds() / 60,
    "alg": settings.SIMPLE_JWT["ALGORITHM"],
    "keys_match": settings.SIMPLE_JWT["SIGNING_KEY"] == settings.SECRET_KEY,
    "default_permissions": settings.REST_FRAMEWORK["DEFAULT_PERMISSION_CLASSES"],
    "throttles": settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"],
    "checks": sorted({{m.id for m in run_checks(include_deployment_checks=True)}}),
}}))
"""
    STRONG_KEY = "prod-k9Zx2Qm7Lp4Wv8Rt6Yn3Bc5Hd1Fg0Js-k9Zx2Qm7Lp4Wv8Rt6Yn3Bc5Hd1Fg0Js"

    def run_probe(self, **env):
        clean = {k: v for k, v in os.environ.items()
                 if not k.startswith(("DJANGO_", "DATABASE_")) and k not in ("SECRET_KEY",)}
        return subprocess.run(
            [sys.executable, "-I", "-c", self.PROBE.format(base=str(settings.BASE_DIR))],
            env={**clean, **env}, capture_output=True, text=True, timeout=120,
        )

    def production(self, **env):
        r = self.run_probe(DJANGO_DEBUG="False", DJANGO_SECRET_KEY=self.STRONG_KEY,
                           DJANGO_ALLOWED_HOSTS="api.example.com", DJANGO_BEHIND_PROXY="True", **env)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_refuses_to_start_in_production_with_the_public_default_secret_key(self):
        r = self.run_probe(DJANGO_DEBUG="False")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY", r.stderr)

        r = self.run_probe(DJANGO_DEBUG="False", DJANGO_SECRET_KEY="django-insecure-secret-key")
        self.assertNotEqual(r.returncode, 0)

    def test_debug_is_off_unless_asked_for(self):
        r = self.run_probe(DJANGO_SECRET_KEY=self.STRONG_KEY, DJANGO_ALLOWED_HOSTS="api.example.com")
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        self.assertIs(json.loads(r.stdout.strip().splitlines()[-1])["DEBUG"], False)

    def test_production_forces_https_and_secure_cookies(self):
        conf = self.production()
        self.assertIs(conf["DEBUG"], False)
        self.assertIs(conf["ssl_redirect"], True)
        self.assertGreaterEqual(conf["hsts"], 31_536_000)
        self.assertIs(conf["session_secure"], True)
        self.assertIs(conf["csrf_secure"], True)
        self.assertEqual(conf["proxy"], ["HTTP_X_FORWARDED_PROTO", "https"])
        self.assertEqual(conf["hosts"], ["api.example.com"])  # no wildcard, nothing implicit

    def test_secure_defaults_for_passwords_and_tokens(self):
        conf = self.production()
        self.assertTrue(conf["hasher"].endswith("Argon2PasswordHasher"))
        self.assertEqual(conf["validators"], [
            "UserAttributeSimilarityValidator", "MinimumLengthValidator",
            "CommonPasswordValidator", "NumericPasswordValidator", "CharacterMixValidator",
        ])
        self.assertLessEqual(conf["access_minutes"], 15)
        self.assertEqual(conf["alg"], "HS256")
        self.assertTrue(conf["keys_match"])
        self.assertEqual(conf["default_permissions"], ["rest_framework.permissions.IsAuthenticated"])
        self.assertEqual(set(conf["throttles"]), {"auth", "email"})

    def test_django_deployment_check_finds_no_security_problems(self):
        # W005/W021 (HSTS for sub-domains / preload) are deliberate opt-ins, off by default here.
        security = [c for c in self.production()["checks"] if c.startswith("security.")]
        self.assertEqual(sorted(set(security) - {"security.W005", "security.W021"}), [])
