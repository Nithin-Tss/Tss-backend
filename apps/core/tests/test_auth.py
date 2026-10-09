"""
Authentication: sign-up -> sign-in -> use the API -> refresh, with JWTs and
hashed passwords.

    python manage.py test apps.core --settings=config.test_settings
"""
from datetime import timedelta

import jwt
from django.conf import settings
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import AccessToken

from apps.identity.models import User

REAL_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]

SIGNUP = {
    "firstName": "Asha", "lastName": "Rao", "email": "Asha.Rao@Example.com",
    "password": "Sturdy-pass-123", "confirmPassword": "Sturdy-pass-123",
    "mobileNumber": "9876543210", "agreeTerms": True,
}


@override_settings(PASSWORD_HASHERS=REAL_HASHERS)
class AuthFlowTest(APITestCase):
    def setUp(self):
        cache.clear()  # reset the sign-in rate limit between tests

    def signup(self, **changes):
        return self.client.post("/api/v1/auth/signup/", {**SIGNUP, **changes}, format="json")

    def login(self, email="asha.rao@example.com", password="Sturdy-pass-123", **extra):
        return self.client.post(
            "/api/v1/auth/login/", {"email": email, "password": password, **extra}, format="json"
        )

    def me(self, token):
        return self.client.get("/api/v1/auth/me/", HTTP_AUTHORIZATION=f"Bearer {token}")

    # ---------------------------------------------------------- the main flow

    def test_signup_then_login_then_use_the_api(self):
        r = self.signup()
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()
        self.assertEqual(body["user"]["email"], "asha.rao@example.com")
        self.assertEqual(set(body) >= {"access", "refresh", "user", "stores"}, True)
        self.assertEqual(self.me(body["access"]).status_code, 200)

        r = self.login(email="ASHA.RAO@example.com")  # email is case-insensitive
        self.assertEqual(r.status_code, 200, r.content)
        access, refresh = r.json()["access"], r.json()["refresh"]

        r = self.me(access)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["user"]["firstName"], "Asha")

        # the access token is a real JWT for this user, valid for 15 minutes
        claims = jwt.decode(access, settings.SECRET_KEY, algorithms=["HS256"])
        self.assertEqual(claims["token_type"], "access")
        self.assertEqual(claims["user_id"], body["user"]["id"])
        self.assertEqual(claims["exp"] - claims["iat"], 15 * 60)

        # refresh gives a new working pair
        r = self.client.post("/api/v1/auth/refresh/", {"refresh": refresh}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.me(r.json()["access"]).status_code, 200)

    # ------------------------------------------------------------- passwords

    def test_password_is_hashed_with_argon2(self):
        self.signup()
        stored = User.objects.get(email="asha.rao@example.com").password
        self.assertTrue(stored.startswith("argon2$"), stored[:20])
        self.assertNotIn("Sturdy-pass-123", stored)

    def test_old_pbkdf2_password_still_works_and_is_upgraded(self):
        user = User(email="old@example.com", first_name="Old", last_name="User")
        with self.settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.PBKDF2PasswordHasher"]):
            user.set_password("Legacy-pass-456")
            user.save()
        self.assertTrue(user.password.startswith("pbkdf2_sha256$"))

        r = self.login(email="old@example.com", password="Legacy-pass-456")
        self.assertEqual(r.status_code, 200, r.content)
        user.refresh_from_db()
        self.assertTrue(user.password.startswith("argon2$"))

    def test_wrong_password_and_unknown_email_get_the_same_answer(self):
        self.signup()
        wrong = self.login(password="nope-nope-nope")
        unknown = self.login(email="nobody@example.com")
        self.assertEqual(wrong.status_code, 400)
        self.assertEqual(wrong.json(), unknown.json())  # doesn't reveal which emails exist
        self.assertNotIn("access", wrong.json())

    def test_signup_rules(self):
        self.assertEqual(self.signup().status_code, 201)
        self.assertIn("email", self.signup().json())                       # duplicate email
        self.assertIn("password", self.signup(email="b@example.com", password="12345678",
                                              confirmPassword="12345678").json())  # weak
        self.assertIn("confirmPassword", self.signup(email="c@example.com",
                                                     confirmPassword="Different-123").json())

    # ---------------------------------------------------------------- tokens

    def test_bad_tokens_are_rejected(self):
        tokens = self.signup().json()
        user = User.objects.get(email="asha.rao@example.com")

        self.assertEqual(self.client.get("/api/v1/auth/me/").status_code, 401)        # none
        self.assertEqual(self.me(tokens["access"][:-3] + "abc").status_code, 401)     # tampered
        self.assertEqual(self.me(tokens["refresh"]).status_code, 401)                 # wrong type

        forged = jwt.encode(
            jwt.decode(tokens["access"], options={"verify_signature": False}),
            "django-insecure-secret-key", algorithm="HS256",
        )
        self.assertEqual(self.me(forged).status_code, 401)                            # wrong key

        expired = AccessToken.for_user(user)
        expired.set_exp(lifetime=timedelta(seconds=-1))
        self.assertEqual(self.me(str(expired)).status_code, 401)                      # expired

        r = self.client.post("/api/v1/auth/refresh/", {"refresh": tokens["access"]}, format="json")
        self.assertEqual(r.status_code, 401)                                          # access as refresh

    def test_changing_password_signs_out_old_tokens(self):
        tokens = self.signup().json()
        user = User.objects.get(email="asha.rao@example.com")
        user.set_password("Brand-new-pass-789")
        user.save()

        self.assertEqual(self.me(tokens["access"]).status_code, 401)
        r = self.client.post("/api/v1/auth/refresh/", {"refresh": tokens["refresh"]}, format="json")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.login(password="Brand-new-pass-789").status_code, 200)

    def test_remember_me_keeps_the_refresh_token_longer(self):
        self.signup()

        def refresh_days(**extra):
            refresh = self.login(**extra).json()["refresh"]
            claims = jwt.decode(refresh, settings.SECRET_KEY, algorithms=["HS256"])
            return round((claims["exp"] - claims["iat"]) / 86400)

        self.assertEqual(refresh_days(), 7)
        self.assertEqual(refresh_days(rememberMe=True), 30)

    def test_too_many_sign_in_attempts_are_slowed_down(self):
        self.signup()
        codes = [self.login(password="wrong-guess-123").status_code for _ in range(12)]
        self.assertIn(429, codes)
