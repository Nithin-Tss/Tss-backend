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
