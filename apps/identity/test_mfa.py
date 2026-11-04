"""
Two-factor sign-in: TOTP codes, recovery codes, and the login challenge.

    python manage.py test apps.identity.test_mfa --settings=config.test_settings
"""
import base64

from django.core.cache import cache
from rest_framework.test import APITestCase

from apps.identity import mfa
from apps.identity.models import User

PASSWORD = "Sturdy-pass-123"


class TotpTests(APITestCase):
    def test_matches_rfc_6238_vectors(self):
        # RFC 6238 appendix B (SHA-1), shortened to the 6 digits we use
        secret = base64.b32encode(b"12345678901234567890").decode()
        self.assertEqual(mfa.totp_now(secret, at=59), "287082")
        self.assertEqual(mfa.totp_now(secret, at=1111111109), "081804")
        self.assertEqual(mfa.totp_now(secret, at=20000000000), "353130")


class MfaFlowTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("owner@example.com", PASSWORD, first_name="O", last_name="W")

    def sign_in(self, **extra):
        return self.client.post("/api/v1/auth/login/", {"email": "owner@example.com", "password": PASSWORD, **extra}, format="json")

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + self.sign_in().json()["access"])

    def enable(self):
        self.authenticate()
        secret = self.client.post("/api/v1/auth/mfa/setup/").json()["secret"]
        r = self.client.post("/api/v1/auth/mfa/enable/", {"code": mfa.totp_now(secret)}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.client.credentials()
        # The setup code used up its time step; let the next one through
        device = mfa.get_device(self.user)
        device.last_used_step = 0
        device.save()
        return secret, r.json()["recoveryCodes"]

    def test_sign_in_is_unchanged_without_mfa(self):
        r = self.sign_in()
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["user"]["mfaEnabled"])

    def test_setup_needs_a_correct_first_code(self):
        self.authenticate()
        uri = self.client.post("/api/v1/auth/mfa/setup/").json()["otpauthUri"]
        self.assertTrue(uri.startswith("otpauth://totp/"))
        r = self.client.post("/api/v1/auth/mfa/enable/", {"code": "000000"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(mfa.is_enabled(self.user))

    def test_password_alone_is_not_enough_once_enabled(self):
        self.enable()
        r = self.sign_in()
        self.assertEqual(r.status_code, 403)
        self.assertTrue(r.json()["mfaRequired"])
        self.assertNotIn("access", r.json())

    def test_wrong_code_is_refused_and_right_code_signs_in(self):
        secret, _ = self.enable()
        self.assertEqual(self.sign_in(mfaCode="123456").status_code, 400)
        r = self.sign_in(mfaCode=mfa.totp_now(secret))
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()["user"]["mfaEnabled"])

    def test_a_code_cannot_be_replayed(self):
        secret, _ = self.enable()
        code = mfa.totp_now(secret)
        self.assertEqual(self.sign_in(mfaCode=code).status_code, 200)
        self.assertEqual(self.sign_in(mfaCode=code).status_code, 400)

    def test_recovery_code_works_once(self):
        _, recovery = self.enable()
        self.assertEqual(self.sign_in(mfaCode=recovery[0]).status_code, 200)
        self.assertEqual(self.sign_in(mfaCode=recovery[0]).status_code, 400)

    def test_disable_needs_password_and_code(self):
        secret, recovery = self.enable()
        r = self.sign_in(mfaCode=recovery[0])
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + r.json()["access"])
        bad = self.client.post("/api/v1/auth/mfa/disable/", {"password": "nope", "code": recovery[1]}, format="json")
        self.assertEqual(bad.status_code, 400)
        ok = self.client.post("/api/v1/auth/mfa/disable/", {"password": PASSWORD, "code": recovery[1]}, format="json")
        self.assertEqual(ok.status_code, 200)
        self.assertFalse(mfa.is_enabled(self.user))

    def test_mfa_endpoints_need_sign_in(self):
        self.assertIn(self.client.get("/api/v1/auth/mfa/").status_code, (401, 403))
