"""
Default API rate limit.

    python manage.py test apps.core.tests.test_throttling --settings=config.test_settings
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework.settings import api_settings
from rest_framework.test import APITestCase

from apps.core.throttling import DefaultApiThrottle

ME = "/api/v1/auth/me/"


class DefaultThrottleTest(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        User = get_user_model()
        self.alice = User.objects.create_user(email="alice@example.com", password="pw-12345-abc")
        self.bob = User.objects.create_user(email="bob@example.com", password="pw-12345-abc")

    def limit(self, rate):
        return mock.patch.object(DefaultApiThrottle, "THROTTLE_RATES", {**api_settings.DEFAULT_THROTTLE_RATES, "api": rate})

    def get_as(self, user):
        self.client.force_authenticate(user)
        return self.client.get(ME)

    def test_signed_in_requests_are_limited_per_user(self):
        with self.limit("3/min"):
            codes = [self.get_as(self.alice).status_code for _ in range(5)]
        self.assertNotIn(429, codes[:3])
        self.assertEqual(codes[3:], [429, 429])

    def test_one_users_limit_does_not_affect_another(self):
        with self.limit("1/min"):
            self.get_as(self.alice)
            self.assertEqual(self.get_as(self.alice).status_code, 429)
            self.assertNotEqual(self.get_as(self.bob).status_code, 429)

    def test_limited_response_says_when_to_retry(self):
        with self.limit("1/min"):
            self.get_as(self.alice)
            r = self.get_as(self.alice)
        self.assertEqual(r.status_code, 429)
        self.assertIn("Retry-After", r)

    def test_default_rate_is_configured(self):
        self.assertIn("api", api_settings.DEFAULT_THROTTLE_RATES)


class ExistingScopesTest(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_sign_in_attempts_are_limited(self):
        codes = [self.client.post("/api/v1/auth/login/", {"email": "x@example.com", "password": "nope"},
                                  format="json").status_code for _ in range(12)]
        self.assertIn(429, codes)
        self.assertNotIn(429, codes[:10])

    def test_every_scope_has_a_rate(self):
        for scope in ("auth", "email", "storefront", "storefront_checkout", "media_upload", "product_image_upload"):
            self.assertIn(scope, api_settings.DEFAULT_THROTTLE_RATES)

    def test_api_key_limit_raises_throttled(self):
        from rest_framework.exceptions import Throttled
        from django.test import override_settings
        from apps.platform_admin.authentication import _rate_limit

        key = mock.Mock(pk=1)
        with override_settings(API_KEY_RATE_LIMIT_PER_MINUTE=2):
            _rate_limit(key)
            _rate_limit(key)
            with self.assertRaises(Throttled):
                _rate_limit(key)


class LockoutCoverageTest(APITestCase):
    """The per-email lockout also guards /admin/login/ and the change-password check."""

    def setUp(self):
        from apps.identity.models import User
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = User.objects.create_superuser(email="root@example.com", password="Root-pass-123",
                                                  first_name="R", last_name="T")

    def test_admin_login_locks_after_repeated_failures(self):
        from django.test import override_settings
        with override_settings(LOGIN_MAX_FAILURES=3):
            for _ in range(3):
                self.client.post("/admin/login/", {"username": "root@example.com", "password": "bad"})
            r = self.client.post("/admin/login/", {"username": "root@example.com",
                                                   "password": "Root-pass-123", "next": "/admin/"})
        self.assertEqual(r.status_code, 200)  # form re-rendered, not signed in
        self.assertContains(r, "Too many failed sign-in attempts")

    def test_change_password_guessing_locks(self):
        from django.test import override_settings
        self.client.force_authenticate(self.user)
        body = {"currentPassword": "bad", "newPassword": "New-pass-123", "confirmPassword": "New-pass-123"}
        with override_settings(LOGIN_MAX_FAILURES=3):
            for _ in range(3):
                self.client.post("/api/v1/auth/password/", body, format="json")
            body["currentPassword"] = "Root-pass-123"
            r = self.client.post("/api/v1/auth/password/", body, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Too many failed attempts", str(r.data))


class ForwardedForTest(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_changing_x_forwarded_for_does_not_reset_the_sign_in_limit(self):
        codes = [self.client.post("/api/v1/auth/login/", {"email": f"u{i}@example.com", "password": "nope"},
                                  format="json", HTTP_X_FORWARDED_FOR=f"10.0.0.{i}").status_code
                 for i in range(12)]
        self.assertIn(429, codes)


class EmailChangeLockoutTest(APITestCase):
    def setUp(self):
        from apps.identity.models import User
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = User.objects.create_user(email="a@example.com", password="Some-pass-123",
                                             first_name="A", last_name="B")

    def test_email_change_password_guessing_locks(self):
        from django.test import override_settings
        self.client.force_authenticate(self.user)
        with override_settings(LOGIN_MAX_FAILURES=3):
            for _ in range(3):
                self.client.patch("/api/v1/auth/me/", {"email": "new@example.com", "currentPassword": "bad"}, format="json")
            r = self.client.patch("/api/v1/auth/me/", {"email": "new@example.com", "currentPassword": "Some-pass-123"},
                                  format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Too many failed attempts", str(r.data))
