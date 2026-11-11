from unittest import mock

from django.core.cache import cache
from rest_framework.test import APITestCase

from apps.ai_assistant import llm
from apps.ai_assistant.models import AiPageDraft, Conversation, Message
from apps.themes.models import StorePage

from .helpers import TwoStores, call, enable_flag, reply

CHAT = "/api/v1/ai-assistant/chat/"
HERO = {"type": "hero", "settings": {"heading": "Hello"}}


class ViewTestBase(TwoStores, APITestCase):
    def setUp(self):
        cache.clear()
        self.make_stores()
        enable_flag(self.store_a.pk)
        enable_flag(self.store_b.pk)
        self.as_(self.a)

    def chat(self, script, **body):
        with mock.patch.object(llm, "chat", side_effect=script):
            return self.client.post(CHAT, body, format="json")


class ChatEndpointTest(ViewTestBase):
    def test_creates_a_draft_and_returns_reply_conversation_and_changes(self):
        r = self.chat([reply(calls=[call("create_page", {"title": "About", "sections": [HERO]})]), reply("Built it.")],
                      message="create an About page")
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(body["reply"], "Built it.")
        self.assertEqual(body["changes"][0]["action"], "created_page")
        self.assertEqual(StorePage.objects.get().status, StorePage.HIDDEN)
        self.assertEqual(Conversation.objects.get().pk.hex, body["conversation_id"].replace("-", ""))
        self.assertEqual(list(Message.objects.values_list("role", flat=True)), ["user", "assistant"])

    def test_follow_up_sends_the_earlier_turns_and_page_ids(self):
        first = self.chat([reply(calls=[call("create_page", {"title": "About", "sections": [HERO]})]), reply("Built it.")],
                          message="make about").json()
        with mock.patch.object(llm, "chat", return_value=reply("Shortened.")) as chat:
            r = self.client.post(CHAT, {"message": "make the heading shorter", "conversation_id": first["conversation_id"]}, format="json")
        self.assertEqual(r.status_code, 200)
        sent = chat.call_args.args[0]
        self.assertEqual(sent[1]["content"].split("\n")[0], "make about")
        self.assertIn(first["changes"][0]["page_id"], sent[2]["content"])
        self.assertEqual(sent[-1]["content"], "make the heading shorter")

    def test_conversations_belong_to_one_merchant_and_one_person(self):
        first = self.chat([reply("hi")], message="hello").json()
        self.as_(self.b)
        r = self.chat([reply("x")], message="hello", conversation_id=first["conversation_id"])
        self.assertEqual(r.status_code, 404)
        self.assertEqual(Message.objects.count(), 2)

    def test_bad_input(self):
        for body in ({}, {"message": "  "}, {"message": 5}, {"message": "x" * 2001}, {"message": "hi", "conversation_id": "nope"}):
            self.assertEqual(self.chat([reply("x")], **body).status_code, 400, body)

    def test_llm_failure_is_a_clean_503(self):
        r = self.chat(llm.LLMError("The AI service is busy right now. Please try again in a minute."), message="hi")
        self.assertEqual(r.status_code, 503)
        self.assertIn("busy", r.json()["detail"])

    def test_missing_api_key_does_not_crash(self):
        with self.settings(OPENROUTER_API_KEY=""):
            r = self.client.post(CHAT, {"message": "hi"}, format="json")
        self.assertEqual(r.status_code, 503)

    def test_never_leaks_the_api_key(self):
        with self.settings(OPENROUTER_API_KEY="sk-or-secret-value"):
            r = self.chat([reply("hi")], message="hello")
        self.assertNotIn("sk-or-secret-value", r.content.decode())


class AccessTest(ViewTestBase):
    def test_signed_out_is_refused(self):
        from rest_framework.test import APIClient

        r = APIClient().post(CHAT, {"message": "hi"}, format="json")  # the sign-up left a session cookie on self.client
        self.assertIn(r.status_code, (401, 403), r.content)

    def test_off_by_default(self):
        from apps.platform_admin.models import FeatureFlagOverride

        FeatureFlagOverride.objects.filter(store_id=self.store_a.pk).delete()
        with mock.patch.object(llm, "chat") as chat:
            self.assertEqual(self.client.post(CHAT, {"message": "hi"}, format="json").status_code, 403)
        chat.assert_not_called()

    def test_flag_is_per_store(self):
        from apps.platform_admin.models import FeatureFlagOverride

        FeatureFlagOverride.objects.filter(store_id=self.store_b.pk).delete()
        self.assertEqual(self.chat([reply("hi")], message="hi").status_code, 200)
        self.as_(self.b)
        self.assertEqual(self.chat([reply("hi")], message="hi").status_code, 403)

    def test_member_of_another_store_is_refused(self):
        self.use(self.a[0], self.b[1]["storeId"])  # A's token, B's store
        self.assertEqual(self.client.post(CHAT, {"message": "hi"}, format="json").status_code, 403)

    def test_themes_manage_is_enforced_by_the_backend(self):
        from apps.tenancy.models import StoreMembership

        # a staff member whose role lacks themes.manage: simulate by stripping permissions from the membership check
        with mock.patch("apps.core.permissions.membership_permissions", return_value=frozenset({"themes.view"})):
            membership = StoreMembership.objects.get(store=self.store_a)
            membership.role = "staff"
            membership.save()
            r = self.client.post(CHAT, {"message": "hi"}, format="json")
        self.assertEqual(r.status_code, 403)

    def test_throttled_per_store(self):
        with self.settings():
            from apps.ai_assistant.views import StoreThrottle

            with mock.patch.object(StoreThrottle, "get_rate", return_value="2/hour"):
                codes = [self.chat([reply("hi")], message="hi").status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])


class DraftEndpointsTest(ViewTestBase):
    def make(self):
        return self.chat([reply(calls=[call("create_page", {"title": "About", "sections": [HERO]})]), reply("ok")],
                         message="go").json()["changes"][0]["page_id"]

    def test_publish(self):
        page_id = self.make()
        r = self.client.post(f"/api/v1/ai-assistant/drafts/{page_id}/publish/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(StorePage.objects.get(pk=page_id).status, StorePage.VISIBLE)

    def test_discard(self):
        page_id = self.make()
        self.assertEqual(self.client.post(f"/api/v1/ai-assistant/drafts/{page_id}/discard/").status_code, 200)
        self.assertFalse(StorePage.objects.filter(pk=page_id).exists())
        self.assertEqual(self.client.post(f"/api/v1/ai-assistant/drafts/{page_id}/discard/").status_code, 404)

    def test_other_merchant_cannot_publish_or_discard(self):
        page_id = self.make()
        self.as_(self.b)
        for action in ("publish", "discard"):
            self.assertEqual(self.client.post(f"/api/v1/ai-assistant/drafts/{page_id}/{action}/").status_code, 404)
        self.assertTrue(AiPageDraft.objects.filter(page_id=page_id).exists())
        self.assertEqual(StorePage.objects.get(pk=page_id).status, StorePage.HIDDEN)

    def test_endpoints_need_the_flag(self):
        page_id = self.make()
        from apps.platform_admin.models import FeatureFlagOverride

        FeatureFlagOverride.objects.filter(store_id=self.store_a.pk).update(enabled=False)
        self.assertEqual(self.client.post(f"/api/v1/ai-assistant/drafts/{page_id}/publish/").status_code, 403)
