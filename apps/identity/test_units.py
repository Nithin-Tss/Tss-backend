"""
Unit tests for the identity building blocks, called directly rather than through the API:
the user manager, password rules, sign-in lockout and the emailed links.

    python manage.py test apps.identity.test_units --settings=config.test_settings
"""
from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core import signing
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings

from apps.identity import email_links, lockout
from apps.identity.models import User, normalize_email
from apps.identity.password_validation import CharacterMixValidator


class NormalizeEmailTests(SimpleTestCase):
    def test_lowercases_and_trims(self):
        self.assertEqual(normalize_email("  Asha.Rao@Example.COM "), "asha.rao@example.com")

    def test_none_and_blank_become_empty(self):
        self.assertEqual(normalize_email(None), "")
        self.assertEqual(normalize_email("   "), "")


class UserManagerTests(TestCase):
    def test_create_user_normalizes_email_and_hashes_password(self):
        user = User.objects.create_user("Asha@Example.com", "Sturdy-pass-123", first_name="A", last_name="R")
        self.assertEqual(user.email, "asha@example.com")
        self.assertNotEqual(user.password, "Sturdy-pass-123")
        self.assertTrue(user.check_password("Sturdy-pass-123"))
        self.assertFalse(user.is_owner)

    def test_create_user_without_password_has_unusable_password(self):
        user = User.objects.create_user("a@example.com", first_name="A", last_name="R")
        self.assertFalse(user.has_usable_password())

    def test_create_user_needs_an_email(self):
        with self.assertRaises(ValueError):
            User.objects.create_user("  ", "pw", first_name="A", last_name="R")

    def test_create_superuser_is_a_platform_admin(self):
        user = User.objects.create_superuser("root@example.com", "Sturdy-pass-123", first_name="R", last_name="T")
        self.assertTrue(user.is_owner)
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.has_perm("anything"))
        self.assertTrue(user.has_module_perms("tenancy"))

    def test_create_superuser_needs_a_password(self):
        with self.assertRaises(ValueError):
            User.objects.create_superuser("root@example.com", None, first_name="R", last_name="T")

    def test_get_by_natural_key_ignores_letter_case(self):
        user = User.objects.create_user("asha@example.com", "pw", first_name="A", last_name="R")
        self.assertEqual(User.objects.get_by_natural_key("  ASHA@Example.com "), user)

    def test_ordinary_user_has_no_admin_rights(self):
        user = User.objects.create_user("a@example.com", "pw", first_name="A", last_name="R")
        self.assertFalse(user.is_staff)
        self.assertFalse(user.has_perm("anything"))
        self.assertFalse(user.has_perms(["a", "b"]))
        self.assertFalse(user.has_module_perms("tenancy"))
        self.assertTrue(user.is_active)

    def test_names_and_string_form(self):
        user = User(email="a@example.com", first_name="Asha", last_name="Rao")
        self.assertEqual(user.get_full_name(), "Asha Rao")
        self.assertEqual(user.get_short_name(), "Asha")
        self.assertEqual(str(user), "a@example.com")

    def test_clean_normalizes_the_email(self):
        user = User(email=" A@Example.com ", first_name="A", last_name="R")
        user.clean()
        self.assertEqual(user.email, "a@example.com")


class CharacterMixValidatorTests(SimpleTestCase):
    validator = CharacterMixValidator()

    def codes(self, password):
        with self.assertRaises(ValidationError) as ctx:
            self.validator.validate(password)
        return {e.code for e in ctx.exception.error_list}

    def test_accepts_a_password_with_every_kind_of_character(self):
        self.validator.validate("Sturdy-pass-123")

    def test_each_missing_kind_is_reported(self):
        self.assertEqual(self.codes("sturdy-pass-123"), {"password_no_upper"})
        self.assertEqual(self.codes("STURDY-PASS-123"), {"password_no_lower"})
        self.assertEqual(self.codes("Sturdy-pass-abc"), {"password_no_number"})
        self.assertEqual(self.codes("SturdyPass123"), {"password_no_special"})

    def test_all_problems_are_reported_together(self):
        self.assertEqual(
            self.codes(""),
            {"password_no_upper", "password_no_lower", "password_no_number", "password_no_special"},
        )

    def test_underscore_and_space_count_as_special(self):
        self.validator.validate("Sturdy pass 123")
        self.validator.validate("Sturdy_pass_123")

    def test_help_text_names_every_rule(self):
        text = self.validator.get_help_text().lower()
        for word in ("uppercase", "lowercase", "number", "special"):
            self.assertIn(word, text)


@override_settings(LOGIN_MAX_FAILURES=3, LOGIN_LOCKOUT_SECONDS=60)
class LockoutTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_not_locked_before_the_limit(self):
        for _ in range(2):
            lockout.record_failure("a@example.com")
        self.assertFalse(lockout.is_locked("a@example.com"))

    def test_locked_at_the_limit(self):
        for _ in range(3):
            lockout.record_failure("a@example.com")
        self.assertTrue(lockout.is_locked("a@example.com"))

    def test_email_letter_case_and_spaces_share_one_counter(self):
        lockout.record_failure("A@Example.com")
        lockout.record_failure(" a@example.COM ")
        lockout.record_failure("a@example.com")
        self.assertTrue(lockout.is_locked("a@EXAMPLE.com"))

    def test_other_emails_are_unaffected(self):
        for _ in range(3):
            lockout.record_failure("a@example.com")
        self.assertFalse(lockout.is_locked("b@example.com"))

    def test_clear_resets_the_count(self):
        for _ in range(3):
            lockout.record_failure("a@example.com")
        lockout.clear("a@example.com")
        self.assertFalse(lockout.is_locked("a@example.com"))

    def test_clear_on_an_unknown_email_is_harmless(self):
        lockout.clear("nobody@example.com")

    def test_counter_restarts_if_the_entry_expires_between_add_and_incr(self):
        with mock.patch.object(lockout.cache, "incr", side_effect=ValueError):
            lockout.record_failure("a@example.com")
        self.assertEqual(cache.get(lockout._key("a@example.com")), 1)

    def test_key_does_not_contain_the_email(self):
        self.assertNotIn("a@example.com", lockout._key("a@example.com"))


class EmailLinkTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("asha@example.com", "pw", first_name="Asha", last_name="Rao")

    def query(self, link):
        return parse_qs(urlparse(link).query)

    # ---- password reset

    def test_reset_link_points_at_the_frontend(self):
        link = email_links.password_reset_link(self.user)
        self.assertTrue(link.startswith(settings.FRONTEND_URL.rstrip("/") + "/auth/reset-password?"))
        self.assertEqual(set(self.query(link)), {"uid", "token"})

    def test_reset_link_round_trips_to_the_user(self):
        q = self.query(email_links.password_reset_link(self.user))
        self.assertEqual(email_links.user_for_password_reset(q["uid"][0], q["token"][0]), self.user)

    def test_reset_link_is_single_use(self):
        q = self.query(email_links.password_reset_link(self.user))
        self.user.set_password("New-pass-456!")
        self.user.save()
        self.assertIsNone(email_links.user_for_password_reset(q["uid"][0], q["token"][0]))

    def test_reset_rejects_a_wrong_token_garbage_and_unknown_user(self):
        q = self.query(email_links.password_reset_link(self.user))
        uid = q["uid"][0]
        self.assertIsNone(email_links.user_for_password_reset(uid, "wrong-token"))
        self.assertIsNone(email_links.user_for_password_reset("!!!", q["token"][0]))
        self.assertIsNone(email_links.user_for_password_reset("", ""))

        gone = User.objects.create_user("gone@example.com", "pw", first_name="G", last_name="G")
        gone_q = self.query(email_links.password_reset_link(gone))
        gone.delete()
        self.assertIsNone(email_links.user_for_password_reset(gone_q["uid"][0], gone_q["token"][0]))

    def test_one_users_token_does_not_open_another_users_reset(self):
        other = User.objects.create_user("b@example.com", "pw", first_name="B", last_name="B")
        mine = self.query(email_links.password_reset_link(self.user))
        theirs = self.query(email_links.password_reset_link(other))
        self.assertIsNone(email_links.user_for_password_reset(mine["uid"][0], theirs["token"][0]))

    def test_reset_token_is_django_s_generator(self):
        q = self.query(email_links.password_reset_link(self.user))
        self.assertTrue(default_token_generator.check_token(self.user, q["token"][0]))

    # ---- email verification

    def test_verification_link_round_trips_to_the_user(self):
        link = email_links.email_verification_link(self.user)
        self.assertTrue(link.startswith(settings.FRONTEND_URL.rstrip("/") + "/auth/verify-email?"))
        token = self.query(link)["token"][0]
        self.assertEqual(email_links.user_for_email_verification(token), self.user)

    def test_verification_rejects_garbage_and_wrong_salt(self):
        self.assertIsNone(email_links.user_for_email_verification("garbage"))
        forged = signing.dumps({"user": str(self.user.pk), "email": self.user.email}, salt="other-salt")
        self.assertIsNone(email_links.user_for_email_verification(forged))

    def test_verification_link_dies_when_the_email_changes(self):
        token = self.query(email_links.email_verification_link(self.user))["token"][0]
        self.user.email = "new@example.com"
        self.user.save()
        self.assertIsNone(email_links.user_for_email_verification(token))

    def test_verification_link_expires(self):
        token = self.query(email_links.email_verification_link(self.user))["token"][0]
        with override_settings(EMAIL_VERIFICATION_TIMEOUT=-1):
            self.assertIsNone(email_links.user_for_email_verification(token))

    def test_verification_for_a_deleted_user_is_none(self):
        token = self.query(email_links.email_verification_link(self.user))["token"][0]
        self.user.delete()
        self.assertIsNone(email_links.user_for_email_verification(token))

    # ---- sending

    def test_reset_email_goes_to_the_user_with_the_link(self):
        with mock.patch.object(email_links, "send_email_async") as send:
            email_links.send_password_reset_email(self.user)
        subject, body, recipients = send.call_args.args
        self.assertEqual(subject, "Reset your password")
        self.assertEqual(recipients, ["asha@example.com"])
        self.assertIn("/auth/reset-password?", body)
        self.assertIn("Hi Asha", body)

    def test_verification_email_goes_to_the_user_with_the_link(self):
        with mock.patch.object(email_links, "send_email_async") as send:
            email_links.send_verification_email(self.user)
        subject, body, recipients = send.call_args.args
        self.assertEqual(subject, "Verify your email address")
        self.assertEqual(recipients, ["asha@example.com"])
        self.assertIn("/auth/verify-email?token=", body)

    def test_a_failing_mailer_never_breaks_the_caller(self):
        with mock.patch.object(email_links, "send_email_async", side_effect=RuntimeError("smtp down")):
            email_links.send_password_reset_email(self.user)
            email_links.send_verification_email(self.user)
