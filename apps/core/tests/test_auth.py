"""
Authentication: sign-up -> sign-in -> use the API -> refresh -> sign-out,
with JWTs, Django sessions and hashed passwords.

    python manage.py test apps.core --settings=config.test_settings
"""
from datetime import timedelta

import jwt
from django.conf import settings
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient, APITestCase
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
        self.client.cookies.clear()  # drop the session cookie from sign-up: tokens only

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

    # ------------------------------------------------------ sign-out, rotation

    def test_refresh_token_is_single_use(self):
        refresh = self.signup().json()["refresh"]

        first = self.client.post("/api/v1/auth/refresh/", {"refresh": refresh}, format="json")
        self.assertEqual(first.status_code, 200, first.content)
        again = self.client.post("/api/v1/auth/refresh/", {"refresh": refresh}, format="json")
        self.assertEqual(again.status_code, 401)  # reused (e.g. stolen) token

        newer = self.client.post("/api/v1/auth/refresh/", {"refresh": first.json()["refresh"]}, format="json")
        self.assertEqual(newer.status_code, 200)

    def test_logout_revokes_refresh_token_and_ends_session(self):
        tokens = self.signup().json()
        self.assertEqual(self.client.get("/api/v1/auth/me/").status_code, 200)  # session cookie

        r = self.client.post("/api/v1/auth/logout/", {"refresh": tokens["refresh"]}, format="json")
        self.assertEqual(r.status_code, 204)

        r = self.client.post("/api/v1/auth/refresh/", {"refresh": tokens["refresh"]}, format="json")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.me(tokens["access"]).status_code, 401)            # access token: at once
        self.client.credentials()
        self.assertEqual(self.client.get("/api/v1/auth/me/").status_code, 401)  # session cookie gone

    def test_logout_ends_only_that_sign_in(self):
        self.signup()
        phone = self.login().json()
        laptop = self.login().json()

        self.client.post("/api/v1/auth/logout/", {"refresh": phone["refresh"]}, format="json")
        self.assertEqual(self.me(phone["access"]).status_code, 401)
        self.assertEqual(self.me(laptop["access"]).status_code, 200)

    def test_logout_always_succeeds(self):
        r = self.client.post("/api/v1/auth/logout/", {}, format="json")
        self.assertEqual(r.status_code, 204)
        r = self.client.post("/api/v1/auth/logout/", {"refresh": "garbage"}, format="json",
                             HTTP_AUTHORIZATION="Bearer expired-or-garbage")
        self.assertEqual(r.status_code, 204)

    # ------------------------------------------------------- change password

    def test_change_password(self):
        tokens = self.signup().json()
        self.client.cookies.clear()
        other_device = APIClient()
        other_device.force_login(User.objects.get(email="asha.rao@example.com"))
        self.assertEqual(other_device.get("/api/v1/auth/me/").status_code, 200)

        def change(**body):
            return self.client.post(
                "/api/v1/auth/password/",
                {"currentPassword": "Sturdy-pass-123", "newPassword": "Brand-new-pass-789",
                 "confirmPassword": "Brand-new-pass-789", **body},
                format="json", HTTP_AUTHORIZATION=f"Bearer {tokens['access']}",
            )

        self.assertIn("currentPassword", change(currentPassword="wrong-one-123").json())
        self.assertIn("confirmPassword", change(confirmPassword="Different-123").json())
        self.assertIn("newPassword", change(newPassword="12345678", confirmPassword="12345678").json())

        r = change()
        self.assertEqual(r.status_code, 200, r.content)
        self.assertNotIn("sessionid", r.cookies)  # token clients get no cookie session
        self.assertEqual(self.me(r.json()["access"]).status_code, 200)           # this device: new tokens
        self.assertEqual(self.me(tokens["access"]).status_code, 401)             # old tokens: out
        self.assertEqual(other_device.get("/api/v1/auth/me/").status_code, 401)  # other sessions: out
        self.assertEqual(self.login(password="Brand-new-pass-789").status_code, 200)


@override_settings(PASSWORD_HASHERS=REAL_HASHERS)
class SessionAuthTest(APITestCase):
    """Sign-in also starts a Django session (cookie), usable instead of a JWT."""

    def setUp(self):
        cache.clear()
        self.client = APIClient(enforce_csrf_checks=True)
        User.objects.create_user(
            email="ravi@example.com", password="Sturdy-pass-123", first_name="Ravi", last_name="K",
        )

    def login(self, **extra):
        return self.client.post(
            "/api/v1/auth/login/",
            {"email": "ravi@example.com", "password": "Sturdy-pass-123", **extra},
            format="json",
        )

    def test_login_sets_session_cookie_that_signs_requests_in(self):
        r = self.login()
        self.assertEqual(r.status_code, 200, r.content)
        cookie = r.cookies["sessionid"]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertFalse(cookie["max-age"])  # ends when the browser closes

        r = self.client.get("/api/v1/auth/me/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["user"]["email"], "ravi@example.com")
        self.assertIsNotNone(User.objects.get(email="ravi@example.com").last_login)

    def test_remember_me_keeps_session_for_30_days(self):
        cookie = self.login(rememberMe=True).cookies["sessionid"]
        self.assertEqual(int(cookie["max-age"]), 30 * 24 * 3600)

    def test_session_writes_need_csrf_token(self):
        self.login()
        body = {"currentPassword": "Sturdy-pass-123", "newPassword": "Brand-new-pass-789",
                "confirmPassword": "Brand-new-pass-789"}

        r = self.client.post("/api/v1/auth/password/", body, format="json")
        self.assertEqual(r.status_code, 403)  # no CSRF token: refused

        csrf = self.client.cookies.get("csrftoken")  # set at sign-in
        self.assertIsNotNone(csrf)
        r = self.client.post("/api/v1/auth/password/", body, format="json",
                             HTTP_X_CSRFTOKEN=csrf.value)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.client.get("/api/v1/auth/me/").status_code, 200)  # still signed in


class UserModelAndAdminTest(APITestCase):
    def test_create_user_and_superuser(self):
        user = User.objects.create_user(email="A@EXAMPLE.com", password="x-Strong-123",
                                        first_name="A", last_name="B")
        self.assertEqual(user.email, "a@example.com")  # stored lower-case
        self.assertTrue(user.is_active)
        self.assertFalse(user.is_staff or user.has_perm("identity.view_user"))
        self.assertTrue(user.check_password("x-Strong-123"))

        no_password = User.objects.create_user(email="c@example.com", first_name="C", last_name="D")
        self.assertFalse(no_password.has_usable_password())

        admin = User.objects.create_superuser(email="root@example.com", password="Root-pass-123",
                                              first_name="R", last_name="T")
        self.assertTrue(admin.is_staff and admin.is_superuser and admin.has_perm("anything"))

        with self.assertRaises(ValueError):
            User.objects.create_user(email="", password="x")

    def test_admin_site_signs_in_with_session(self):
        User.objects.create_superuser(email="root@example.com", password="Root-pass-123",
                                      first_name="R", last_name="T")
        User.objects.create_user(email="someone@example.com", password="Some-pass-123",
                                 first_name="S", last_name="O")

        self.assertEqual(self.client.get("/admin/").status_code, 302)  # to the login page
        self.assertTrue(self.client.login(username="someone@example.com", password="Some-pass-123"))
        self.assertEqual(self.client.get("/admin/").status_code, 302)  # not an admin
        self.client.logout()

        r = self.client.post("/admin/login/", {"username": "root@example.com",
                                               "password": "Root-pass-123", "next": "/admin/"})
        self.assertEqual(r.status_code, 302)
        r = self.client.get("/admin/identity/user/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "someone@example.com")
        user = User.objects.get(email="someone@example.com")
        self.assertEqual(self.client.get(f"/admin/identity/user/{user.pk}/change/").status_code, 200)
        self.assertEqual(self.client.get("/admin/identity/user/add/").status_code, 200)

        r = self.client.post("/admin/identity/user/add/", {
            "email": "new@example.com", "first_name": "N", "last_name": "U",
            "usable_password": "true", "password1": "New-admin-pass-1", "password2": "New-admin-pass-1",
        })
        self.assertEqual(r.status_code, 302, r.content[:2000])
        self.assertTrue(User.objects.get(email="new@example.com").check_password("New-admin-pass-1"))

    def test_email_letter_case_never_matters(self):
        User.objects.create_user(email="Ravi.K@Example.COM", password="Sturdy-pass-123",
                                 first_name="R", last_name="K")
        self.assertTrue(User.objects.filter(email="ravi.k@example.com").exists())

        cache.clear()
        r = self.client.post("/api/v1/auth/login/", {"email": "RAVI.k@example.com",
                                                     "password": "Sturdy-pass-123"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(self.client.login(username="Ravi.K@Example.com", password="Sturdy-pass-123"))

    def test_admin_rejects_email_that_differs_only_in_case(self):
        User.objects.create_superuser(email="root@example.com", password="Root-pass-123",
                                      first_name="R", last_name="T")
        self.client.login(username="root@example.com", password="Root-pass-123")
        r = self.client.post("/admin/identity/user/add/", {
            "email": "ROOT@example.com", "first_name": "N", "last_name": "U",
            "usable_password": "true", "password1": "New-admin-pass-1", "password2": "New-admin-pass-1",
        })
        self.assertEqual(r.status_code, 200)  # form shown again with the error
        self.assertContains(r, "A user with this email already exists.")
        self.assertEqual(User.objects.count(), 1)

    def test_admin_can_change_a_users_password(self):
        User.objects.create_superuser(email="root@example.com", password="Root-pass-123",
                                      first_name="R", last_name="T")
        user = User.objects.create_user(email="s@example.com", password="Old-pass-123",
                                        first_name="S", last_name="O")
        self.client.login(username="root@example.com", password="Root-pass-123")
        url = f"/admin/identity/user/{user.pk}/password/"
        self.assertEqual(self.client.get(url).status_code, 200)
        r = self.client.post(url, {"usable_password": "true", "password1": "Fresh-pass-456",
                                   "password2": "Fresh-pass-456"})
        self.assertEqual(r.status_code, 302, r.content[:1500])
        user.refresh_from_db()
        self.assertTrue(user.check_password("Fresh-pass-456"))
