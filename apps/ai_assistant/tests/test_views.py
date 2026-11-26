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


class ConversationEndpointsTest(ViewTestBase):
    BASE = "/api/v1/ai-assistant/conversations/"

    def test_list_read_and_delete(self):
        made = self.chat([reply(calls=[call("create_page", {"title": "About", "sections": [HERO]})]), reply("Built it.")],
                         message="make an About page").json()
        [row] = self.client.get(self.BASE).json()
        self.assertEqual((row["id"], row["message_count"], row["preview"]), (made["conversation_id"], 2, "make an About page"))

        detail = self.client.get(f"{self.BASE}{made['conversation_id']}/").json()
        self.assertEqual([m["role"] for m in detail["messages"]], ["user", "assistant"])
        self.assertEqual(detail["messages"][1]["changes"][0]["action"], "created_page")

        self.assertEqual(self.client.delete(f"{self.BASE}{made['conversation_id']}/").status_code, 204)
        self.assertFalse(Conversation.objects.exists())

    def test_another_store_or_person_sees_nothing(self):
        made = self.chat([reply("hi")], message="hello").json()
        self.as_(self.b)
        self.assertEqual(self.client.get(self.BASE).json(), [])
        self.assertEqual(self.client.get(f"{self.BASE}{made['conversation_id']}/").status_code, 404)
        self.assertEqual(self.client.delete(f"{self.BASE}{made['conversation_id']}/").status_code, 404)
        self.assertTrue(Conversation.objects.exists())

    def test_bad_id_is_404(self):
        self.assertEqual(self.client.get(f"{self.BASE}nope/").status_code, 404)

    def test_drafts_list(self):
        page_id = self.chat([reply(calls=[call("create_page", {"title": "About", "sections": [HERO]})]), reply("ok")],
                            message="go").json()["changes"][0]["page_id"]
        [draft] = self.client.get("/api/v1/ai-assistant/drafts/").json()
        self.assertEqual((draft["page_id"], draft["title"], draft["created_by_ai"]), (page_id, "About", True))
        self.as_(self.b)
        self.assertEqual(self.client.get("/api/v1/ai-assistant/drafts/").json(), [])


class WsgiFastApiPathsTest(ViewTestBase):
    """Production runs gunicorn + config.wsgi: the /fastapi/ai-assistant paths must answer there, not 404."""

    def wsgi(self, method, path, body=b"", headers=None):
        import io
        import json as jsonlib

        from django.core import signals
        from django.db import close_old_connections

        from config import wsgi

        environ = {
            "REQUEST_METHOD": method, "PATH_INFO": path, "SCRIPT_NAME": "", "QUERY_STRING": "",
            "SERVER_NAME": "testserver", "SERVER_PORT": "80", "SERVER_PROTOCOL": "HTTP/1.1", "REMOTE_ADDR": "127.0.0.1",
            "wsgi.version": (1, 0), "wsgi.url_scheme": "http", "wsgi.input": io.BytesIO(body), "wsgi.errors": io.StringIO(),
            "wsgi.multithread": False, "wsgi.multiprocess": False, "wsgi.run_once": False,
            "CONTENT_LENGTH": str(len(body)), "CONTENT_TYPE": "application/json" if body else "",
            **(headers or {}),
        }
        status = []
        # Like Django's test client: don't let the request signals close the test's transaction
        signals.request_started.disconnect(close_old_connections)
        signals.request_finished.disconnect(close_old_connections)
        try:
            chunks = b"".join(wsgi.application(environ, lambda s, h, exc_info=None: status.append(s)))
        finally:
            signals.request_started.connect(close_old_connections)
            signals.request_finished.connect(close_old_connections)
        code = int(status[0].split()[0])
        return code, (jsonlib.loads(chunks) if chunks else None)

    def auth(self, who=None):
        who = who or self.a
        return {"HTTP_AUTHORIZATION": f"Bearer {who[0]}", "HTTP_X_STORE_ID": who[1]["storeId"]}

    def test_prefix_matches_the_fastapi_app(self):
        from apps.ai_assistant.fastapi_app import PREFIX
        from config import wsgi

        self.assertEqual(wsgi.AI_FASTAPI_PREFIX, PREFIX)

    def test_path_mapping(self):
        from config.wsgi import ai_assistant_path

        self.assertEqual(ai_assistant_path("/fastapi/ai-assistant/chat"), "/api/v1/ai-assistant/chat/")
        self.assertEqual(ai_assistant_path("/fastapi/ai-assistant/drafts/x/publish/"), "/api/v1/ai-assistant/drafts/x/publish/")
        self.assertEqual(ai_assistant_path("/fastapi/ai-assistant"), "/api/v1/ai-assistant/")
        self.assertIsNone(ai_assistant_path("/fastapi/ai-assistantX/chat"))
        self.assertIsNone(ai_assistant_path("/api/v1/ai-assistant/chat/"))

    def test_fastapi_paths_work_under_wsgi(self):
        import json as jsonlib

        script = [reply(calls=[call("create_page", {"title": "About", "sections": [HERO]})]), reply("Built it.")]
        with mock.patch.object(llm, "chat", side_effect=script):
            code, made = self.wsgi("POST", "/fastapi/ai-assistant/chat", jsonlib.dumps({"message": "make about"}).encode(), self.auth())
        self.assertEqual(code, 200, made)
        self.assertEqual(made["reply"], "Built it.")
        page_id = made["changes"][0]["page_id"]

        code, rows = self.wsgi("GET", "/fastapi/ai-assistant/conversations", headers=self.auth())
        self.assertEqual((code, rows[0]["id"]), (200, made["conversation_id"]))
        code, detail = self.wsgi("GET", f"/fastapi/ai-assistant/conversations/{made['conversation_id']}", headers=self.auth())
        self.assertEqual((code, len(detail["messages"])), (200, 2))
        code, drafts = self.wsgi("GET", "/fastapi/ai-assistant/drafts", headers=self.auth())
        self.assertEqual((code, drafts[0]["page_id"]), (200, page_id))

        code, _ = self.wsgi("POST", f"/fastapi/ai-assistant/drafts/{page_id}/publish", headers=self.auth())
        self.assertEqual(code, 200)
        self.assertEqual(StorePage.objects.get(pk=page_id).status, StorePage.VISIBLE)
        code, _ = self.wsgi("DELETE", f"/fastapi/ai-assistant/conversations/{made['conversation_id']}", headers=self.auth())
        self.assertEqual(code, 204)

    def test_same_access_rules_under_wsgi(self):
        self.assertIn(self.wsgi("GET", "/fastapi/ai-assistant/drafts")[0], (401, 403))
        self.assertEqual(self.wsgi("GET", "/fastapi/ai-assistant/conversations/nope", headers=self.auth())[0], 404)
