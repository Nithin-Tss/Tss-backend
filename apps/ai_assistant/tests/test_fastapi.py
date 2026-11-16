"""
The FastAPI front end (apps/ai_assistant/fastapi_app.py). The app runs its endpoints on other threads, which can't
see a TestCase's open transaction, so this uses a transactional test case.
"""
from unittest import mock

from django.core.cache import cache
from django.db import connections
from rest_framework.test import APITransactionTestCase
from starlette.testclient import TestClient

from apps.ai_assistant import fastapi_app, llm
from apps.ai_assistant.models import AiPageDraft, Conversation, Message
from apps.ai_assistant.views import StoreThrottle
from apps.platform_admin.models import FeatureFlagOverride
from apps.themes.models import StorePage

from .helpers import TwoStores, call, enable_flag, reply

HERO = {"type": "hero", "settings": {"heading": "Hello"}}


class FastApiBase(TwoStores, APITransactionTestCase):
    def setUp(self):
        cache.clear()
        self.snapshot()
        self.make_stores()
        enable_flag(self.store_a.pk)
        enable_flag(self.store_b.pk)
        self.web = TestClient(fastapi_app.app)
        # The app's worker threads keep their connections open; close them so the test database can be dropped
        patch = mock.patch.object(fastapi_app, "close_old_connections", connections.close_all)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.delete_everything)

    def snapshot(self):
        from django.apps import apps

        self.before = {m: set(m._base_manager.values_list("pk", flat=True)) for m in apps.get_models() if m._meta.managed}

    def delete_everything(self):
        """
        Remove what this test added. The end-of-test flush doesn't empty these tables (their names carry a schema), so
        without this the next test finds the same sign-ups. Raw deletes, repeated until the foreign keys allow them.
        """
        from django.db import IntegrityError

        pending = dict(self.before)
        for _ in range(6):
            for model in list(pending):
                try:
                    model._base_manager.exclude(pk__in=pending[model])._raw_delete(using="default")
                    del pending[model]
                except IntegrityError:
                    pass
            if not pending:
                break

    def headers(self, who=None):
        who = who or self.a
        return {"Authorization": f"Bearer {who[0]}", "X-Store-Id": who[1]["storeId"]}

    def get(self, path, who=None):
        return self.web.get(path, headers=self.headers(who))

    def post(self, path, body=None, who=None, script=None):
        with mock.patch.object(llm, "chat", side_effect=script or [reply("hi")]):
            return self.web.post(path, json=body, headers=self.headers(who))

    def build_page(self, who=None):
        script = [reply(calls=[call("create_page", {"title": "About", "sections": [HERO]})]), reply("Built it.")]
        r = self.post("/chat", {"message": "make an About page"}, who, script)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


class ChatTest(FastApiBase):
    def test_chat_builds_a_draft_like_the_drf_endpoint(self):
        body = self.build_page()
        self.assertEqual(body["reply"], "Built it.")
        self.assertEqual(body["changes"][0]["action"], "created_page")
        self.assertEqual(StorePage.objects.get().status, StorePage.HIDDEN)
        self.assertEqual(list(Message.objects.values_list("role", flat=True)), ["user", "assistant"])

    def test_follow_up_continues_the_conversation(self):
        first = self.build_page()
        r = self.post("/chat", {"message": "shorter", "conversation_id": first["conversation_id"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["conversation_id"], first["conversation_id"])
        self.assertEqual(Message.objects.count(), 4)

    def test_bad_input_is_a_400_with_the_same_messages(self):
        for body in ({}, {"message": "  "}, {"message": "x" * 2001}, {"message": "hi", "conversation_id": "nope"}):
            r = self.post("/chat", body)
            self.assertEqual(r.status_code, 400, body)
        self.assertIn("message", self.post("/chat", {}).json())

    def test_llm_failure_is_a_clean_503(self):
        r = self.post("/chat", {"message": "hi"}, script=llm.LLMError("The AI service is busy right now."))
        self.assertEqual(r.status_code, 503)
        self.assertIn("busy", r.json()["detail"])

    def test_throttled_per_store(self):
        with mock.patch.object(StoreThrottle, "get_rate", return_value="2/hour"):
            codes = [self.post("/chat", {"message": "hi"}).status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])


class AccessTest(FastApiBase):
    def test_signed_out_is_401(self):
        r = self.web.post("/chat", json={"message": "hi"}, headers={"X-Store-Id": self.a[1]["storeId"]})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertIn("Bearer", r.headers["www-authenticate"])

    def test_bad_token_is_401(self):
        r = self.web.get("/conversations", headers={"Authorization": "Bearer nonsense", "X-Store-Id": self.a[1]["storeId"]})
        self.assertEqual(r.status_code, 401)

    def test_store_header_is_required(self):
        r = self.web.get("/conversations", headers={"Authorization": f"Bearer {self.a[0]}"})
        self.assertEqual(r.status_code, 400, r.text)

    def test_other_stores_are_refused(self):
        r = self.web.get("/conversations", headers={"Authorization": f"Bearer {self.a[0]}", "X-Store-Id": self.b[1]["storeId"]})
        self.assertEqual(r.status_code, 403)

    def test_off_unless_the_flag_is_on(self):
        FeatureFlagOverride.objects.filter(store_id=self.store_a.pk).delete()
        with mock.patch.object(llm, "chat") as model:
            self.assertEqual(self.post("/chat", {"message": "hi"}).status_code, 403)
        model.assert_not_called()
        self.assertEqual(self.get("/conversations").status_code, 403)

    def test_manage_is_enforced_on_writes_only(self):
        with mock.patch("apps.core.permissions.membership_permissions", return_value=frozenset({"themes.view"})):
            from apps.tenancy.models import StoreMembership

            StoreMembership.objects.filter(store=self.store_a).update(role="staff")
            from apps.tenancy import membership_cache

            membership_cache.clear() if hasattr(membership_cache, "clear") else cache.clear()
            self.assertEqual(self.get("/conversations").status_code, 200)
            self.assertEqual(self.post("/chat", {"message": "hi"}).status_code, 403)

    def test_api_keys_are_refused(self):
        r = self.web.get("/conversations", headers={"Authorization": "Bearer tssk_whatever", "X-Store-Id": self.a[1]["storeId"]})
        self.assertEqual(r.status_code, 401)

    def test_preflight_allows_the_store_header(self):
        r = self.web.options("/chat", headers={
            "Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,x-store-id,content-type",
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["access-control-allow-origin"], "http://localhost:3000")


class ConversationTest(FastApiBase):
    def test_list_and_read(self):
        made = self.build_page()
        listing = self.get("/conversations")
        self.assertEqual(listing.status_code, 200)
        [row] = listing.json()
        self.assertEqual(row["id"], made["conversation_id"])
        self.assertEqual(row["message_count"], 2)
        self.assertEqual(row["preview"], "make an About page")

        detail = self.get(f"/conversations/{made['conversation_id']}").json()
        self.assertEqual([m["role"] for m in detail["messages"]], ["user", "assistant"])
        self.assertEqual(detail["messages"][1]["changes"][0]["action"], "created_page")

    def test_another_store_or_person_sees_nothing(self):
        made = self.build_page()
        self.assertEqual(self.get("/conversations", self.b).json(), [])
        self.assertEqual(self.get(f"/conversations/{made['conversation_id']}", self.b).status_code, 404)
        self.assertEqual(self.web.delete(f"/conversations/{made['conversation_id']}", headers=self.headers(self.b)).status_code, 404)
        self.assertTrue(Conversation.objects.filter(pk=made["conversation_id"]).exists())

    def test_help_assistant_chats_are_not_listed(self):
        from apps.identity.models import User

        Conversation.objects.create(
            store=self.store_a, user=User.objects.get(email="a@example.com"), assistant_type=Conversation.HELP,
        )
        self.assertEqual(self.get("/conversations").json(), [])

    def test_delete(self):
        made = self.build_page()
        r = self.web.delete(f"/conversations/{made['conversation_id']}", headers=self.headers())
        self.assertEqual(r.status_code, 204)
        self.assertFalse(Conversation.objects.exists())
        self.assertEqual(self.get(f"/conversations/{made['conversation_id']}").status_code, 404)

    def test_bad_id_is_404(self):
        self.assertEqual(self.get("/conversations/nope").status_code, 404)


class DraftTest(FastApiBase):
    def test_list_publish_and_discard(self):
        page_id = self.build_page()["changes"][0]["page_id"]
        [draft] = self.get("/drafts").json()
        self.assertEqual((draft["page_id"], draft["title"], draft["created_by_ai"]), (page_id, "About", True))
        self.assertEqual(self.get("/drafts", self.b).json(), [])

        r = self.post(f"/drafts/{page_id}/publish")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(StorePage.objects.get(pk=page_id).status, StorePage.VISIBLE)
        self.assertTrue(r.json()["url"].startswith("http"))
        self.assertEqual(self.get("/drafts").json(), [])

    def test_discard_deletes_a_page_the_assistant_made(self):
        page_id = self.build_page()["changes"][0]["page_id"]
        r = self.post(f"/drafts/{page_id}/discard")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(StorePage.objects.filter(pk=page_id).exists())

    def test_another_store_cannot_touch_a_draft(self):
        page_id = self.build_page()["changes"][0]["page_id"]
        self.assertEqual(self.post(f"/drafts/{page_id}/publish", who=self.b).status_code, 404)
        self.assertEqual(self.post(f"/drafts/{page_id}/discard", who=self.b).status_code, 404)
        self.assertTrue(AiPageDraft.objects.filter(page_id=page_id).exists())

    def test_bad_id_is_404(self):
        self.assertEqual(self.post("/drafts/nope/publish").status_code, 404)


class MountTest(FastApiBase):
    def test_asgi_routes_the_prefix_to_fastapi_and_the_rest_to_django(self):
        import asyncio

        from config import asgi

        sent = []

        async def run(path):
            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                sent.append(message)

            scope = {
                "type": "http", "method": "GET", "path": path, "raw_path": path.encode(), "query_string": b"",
                "headers": [(b"host", b"testserver")], "scheme": "http", "server": ("testserver", 80),
                "client": ("127.0.0.1", 1), "root_path": "", "http_version": "1.1", "asgi": {"version": "3.0"},
            }
            await asgi.application(scope, receive, send)

        asyncio.run(run("/fastapi/ai-assistant/openapi.json"))
        start = next(m for m in sent if m["type"] == "http.response.start")
        self.assertEqual(start["status"], 200)
        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        self.assertIn(b'"/chat"', body)
