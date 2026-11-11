"""
Help Center "Contact support" endpoint (apps/core/support.py). No database: the permission classes are replaced and
the user and store are stand-ins; the email send is mocked.

    python manage.py test apps.core.tests.test_support --settings=config.test_settings
"""
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from rest_framework import exceptions
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core import support
from apps.core.support import SupportContactView

# Only the attribute names the real Store model has (apps/tenancy/models.py): store_id is its primary key
STORE = SimpleNamespace(store_id="11111111-1111-1111-1111-111111111111", store_name="Tea\nShop")
USER = SimpleNamespace(
    pk=7, is_authenticated=True, email="asha@example.com", get_full_name=lambda: "Asha Rao",
)
GOOD = {"subject": "Price problem", "message": "The price does not save when I press Save."}


def call(body, *, user=USER, store=STORE):
    request = APIRequestFactory().post("/api/v1/support/contact/", body, format="json")
    request.store = store  # IsStoreMember would set this
    request.audit = object()  # what IsStoreMember's audit.begin() leaves on the Django request
    force_authenticate(request, user=user)
    with mock.patch.object(SupportContactView, "permission_classes", []):
        response = SupportContactView.as_view()(request)
    return response, request


@override_settings(SUPPORT_EMAIL="help@tss.test")
class SupportContactTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_sends_one_plain_email_with_reply_to_the_user(self):
        with mock.patch.object(support, "send_store_email") as send:
            response, _ = call({**GOOD, "page": "/admin/help/add-product", "transcript": "Me: hi"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"sent": True})
        send.assert_called_once()
        subject, body, recipients = send.call_args.args
        self.assertEqual(recipients, ["help@tss.test"])
        self.assertEqual(send.call_args.kwargs, {"reply_to": "asha@example.com"})
        self.assertEqual(subject, "[Support] Tea Shop: Price problem")
        self.assertIn(GOOD["message"], body)
        self.assertIn("Store ID: 11111111-1111-1111-1111-111111111111", body)
        self.assertIn("Name: Asha Rao", body)
        self.assertIn("Email: asha@example.com", body)
        self.assertIn("Page I came from: /admin/help/add-product", body)
        self.assertIn("--- Chat with the assistant ---\nMe: hi", body)

    def test_identity_comes_from_the_request_not_the_body(self):
        body = {**GOOD, "storeName": "Evil", "storeId": "x", "email": "evil@example.com", "name": "Evil"}
        with mock.patch.object(support, "send_store_email") as send:
            call(body)
        text = send.call_args.args[1]
        self.assertNotIn("Evil", text)
        self.assertNotIn("evil@example.com", text)
        self.assertEqual(send.call_args.kwargs["reply_to"], "asha@example.com")

    def test_email_builds_from_objects_with_only_the_real_model_attributes(self):
        # A class (not a namespace with extras) exposing just Store.store_id / Store.store_name and the User fields
        # the view reads: touching any other attribute, such as store.name or store.pk, raises AttributeError.
        class RealStore:
            __slots__ = ("store_id", "store_name")

            def __init__(self):
                self.store_id = "22222222-2222-2222-2222-222222222222"
                self.store_name = "Real Shop"

        class RealUser:
            __slots__ = ("email",)

            def __init__(self):
                self.email = "real@example.com"

            def get_full_name(self):
                return "Real Person"

        fields = support.clean_request(GOOD)
        subject, body = support.build_support_email(fields=fields, store=RealStore(), user=RealUser())
        self.assertEqual(subject, "[Support] Real Shop: Price problem")
        self.assertIn("Store: Real Shop", body)
        self.assertIn("Store ID: 22222222-2222-2222-2222-222222222222", body)
        self.assertIn("Email: real@example.com", body)

    def test_no_chat_section_without_a_transcript(self):
        with mock.patch.object(support, "send_store_email") as send:
            call(GOOD)
        self.assertNotIn("Chat with the assistant", send.call_args.args[1])
        self.assertIn("Page I came from: -", send.call_args.args[1])

    def test_subject_is_one_line(self):
        with mock.patch.object(support, "send_store_email") as send:
            call({**GOOD, "subject": "Line one\r\nBcc: x@example.com"})
        self.assertNotIn("\n", send.call_args.args[0])

    def test_audit_collector_is_dropped(self):
        with mock.patch.object(support, "send_store_email"):
            _, request = call(GOOD)
        self.assertIsNone(request.audit)

    def test_limits(self):
        cases = [
            ({"subject": "s" * 151}, "subject"),
            ({"message": "m" * 2001}, "message"),
            ({"transcript": "t" * 4001}, "transcript"),
            ({"subject": "   "}, "subject"),
            ({"message": "short"}, "message"),
            ({"subject": 5}, "subject"),
        ]
        for change, field in cases:
            with self.subTest(change=field), mock.patch.object(support, "send_store_email") as send:
                cache.clear()  # each case starts with a fresh throttle counter
                response, _ = call({**GOOD, **change})
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.data)
                send.assert_not_called()

    def test_limits_allow_the_maximum(self):
        body = {"subject": "s" * 150, "message": "m" * 2000, "transcript": "t" * 4000}
        with mock.patch.object(support, "send_store_email") as send:
            response, _ = call(body)
        self.assertEqual(response.status_code, 200)
        send.assert_called_once()

    def test_body_must_be_an_object(self):
        with self.assertRaises(exceptions.ValidationError):
            support.clean_request(["not", "an", "object"])

    def test_send_failure_is_a_502(self):
        with mock.patch.object(support, "send_store_email", side_effect=OSError("smtp down")):
            response, _ = call(GOOD)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.data["detail"], "We couldn't send your message. Please try again.")

    def test_sixth_message_in_an_hour_is_throttled(self):
        with mock.patch.object(support, "send_store_email"):
            statuses = [call(GOOD)[0].status_code for _ in range(6)]
        self.assertEqual(statuses, [200] * 5 + [429])

    def test_throttle_is_per_user(self):
        other = SimpleNamespace(pk=8, is_authenticated=True, email="b@example.com", get_full_name=lambda: "B")
        with mock.patch.object(support, "send_store_email"):
            for _ in range(5):
                call(GOOD)
            self.assertEqual(call(GOOD)[0].status_code, 429)
            self.assertEqual(call(GOOD, user=other)[0].status_code, 200)


class SupportNotConfiguredTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    @override_settings(SUPPORT_EMAIL="")
    def test_empty_support_email_is_a_503_and_sends_nothing(self):
        with mock.patch.object(support, "send_store_email") as send:
            response, _ = call(GOOD)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["detail"], "Support email is not configured.")
        send.assert_not_called()

    @override_settings(SUPPORT_EMAIL="")
    def test_not_configured_does_not_use_up_the_allowance(self):
        with mock.patch.object(support, "send_store_email"):
            statuses = {call(GOOD)[0].status_code for _ in range(8)}
        self.assertEqual(statuses, {503})
