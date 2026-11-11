"""The endpoint, with the database, the model and the store-membership check replaced by fakes."""
import ast
import io
import json
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from rest_framework.permissions import BasePermission
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.assistant import ollama, queries, views
from apps.audit import services as audit
from apps.platform_admin.models import ApiKey

URL = "/api/v1/assistant/chat/"
PERMISSIONS = {"reports.view", "inventory.view", "orders.view"}
seen = []  # the Django requests that reached the permission check


class FakeStoreMember(BasePermission):
    """What IsStoreMember does apart from the database: set the store and register the request for audit."""

    def has_permission(self, request, view):
        request.store = SimpleNamespace(pk="store-1", timezone="Asia/Kolkata", status="active")
        request.membership = SimpleNamespace(role="staff", _permissions=set(self.permissions))
        audit.begin(request)
        seen.append(request._request)
        return True

    permissions = PERMISSIONS


def post(body, user=None, auth=None, **headers):
    request = APIRequestFactory().post(URL, body, format="json", **headers)
    force_authenticate(request, user=user or SimpleNamespace(pk=7, is_authenticated=True), token=auth)
    return views.AssistantChatView.as_view()(request)


@override_settings(ASSISTANT_REAL_MODE=True)
class ChatViewTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        seen.clear()
        FakeStoreMember.permissions = set(PERMISSIONS)
        patches = [
            mock.patch.object(views.AssistantChatView, "permission_classes",
                              [views.IsAuthenticated, views.NoApiKeys, FakeStoreMember]),
            mock.patch.object(views.queries, "alias_configured", return_value=True),
            mock.patch.object(views.queries, "ensure_read_only"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_answers_with_the_tools_used(self):
        with mock.patch.object(views.agent, "run", return_value=("Net sales were ₹26,600.",
                                                              [{"name": "sales_summary", "arguments": {"period": "7d"}}])) as run:
            response = post({"message": "How were sales?"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"answer": "Net sales were ₹26,600.",
                                         "tools": [{"name": "sales_summary", "arguments": {"period": "7d"}},],
                                         "actions": []})
        question, ctx = run.call_args.args
        self.assertEqual(question, "How were sales?")
        self.assertEqual(ctx.store_id, "store-1")  # from the request, not from the body
        self.assertEqual(ctx.timezone_name, "Asia/Kolkata")

    def test_a_store_id_in_the_body_is_ignored(self):
        with mock.patch.object(views.agent, "run", return_value=("ok", [])) as run:
            post({"message": "hi", "store_id": "another-store"})
        self.assertEqual(run.call_args.args[1].store_id, "store-1")

    def test_writes_no_audit_row(self):
        with mock.patch.object(views.agent, "run", return_value=("ok", [])):
            post({"message": "hi"})
        self.assertEqual(len(seen), 1)
        self.assertIsNone(seen[0].audit)  # AuditMiddleware finds no collector, so it saves nothing
        self.assertIsNone(audit.log_request(seen[0], SimpleNamespace(status_code=200)))

    def test_message_rules(self):
        for body in ({}, {"message": ""}, {"message": "   "}, {"message": 5}, {"message": "x" * 501}):
            self.assertEqual(post(body).status_code, 400, body)
        with mock.patch.object(views.agent, "run", return_value=("ok", [])):
            self.assertEqual(post({"message": "x" * 500}).status_code, 200)

    def test_api_keys_are_refused(self):
        key = ApiKey()
        with mock.patch.object(views.agent, "run") as run:
            response = post({"message": "hi"}, auth=key)
        self.assertEqual(response.status_code, 403)
        run.assert_not_called()

    def test_member_with_no_data_permission_can_still_use_the_guide_without_the_database(self):
        FakeStoreMember.permissions = {"products.view"}
        with mock.patch.object(views.agent, "run", return_value=("steps", [])) as run,                 mock.patch.object(views.queries, "alias_configured", return_value=False),                 mock.patch.object(views.queries, "ensure_read_only") as check:
            response = post({"message": "How do I add a product?"})
        self.assertEqual(response.status_code, 200)
        check.assert_not_called()  # no database at all for a guide-only member
        offered = {t.name for t in views.tools.available_tools(run.call_args.args[1].permissions)}
        self.assertEqual(offered, {"admin_help"})

    def test_data_tools_stay_refused_without_their_permission(self):
        ctx = views.tools.Context("store-1", "Asia/Kolkata", frozenset({"products.view"}))
        result, _ = views.tools.run_tool(ctx, "orders_list", {})
        self.assertIn("error", result)

    def test_actions_come_from_the_guide_through_the_whitelist(self):
        def run(question, ctx, **kwargs):
            ctx.open_keys.extend(["customize", "code", "../../etc/passwd", "customize", "http://evil.example"])
            return "1. Open Customize.", [{"name": "admin_help", "arguments": {"topic": "logo"}}]

        with mock.patch.object(views.agent, "run", side_effect=run):
            response = post({"message": "How do I change the logo text?"})
        self.assertEqual(response.data["actions"], [])  # no themes.manage in PERMISSIONS: steps only, no buttons
        FakeStoreMember.permissions = PERMISSIONS | {"themes.manage"}
        with mock.patch.object(views.agent, "run", side_effect=run):
            response = post({"message": "How do I change the logo text?"})
        self.assertEqual(response.data["actions"], [
            {"label": "Open Customize", "path": "/admin/online-store/customize"},
            {"label": "Open Code editor", "path": "/admin/online-store/code"},
        ])

    def test_a_theme_file_button_replaces_the_guide_buttons(self):
        def run(question, ctx, **kwargs):
            ctx.opened_files.append("sections/product-grid.liquid")
            ctx.open_keys.extend(["pages", "code"])
            return "1. Open it.", []

        FakeStoreMember.permissions = PERMISSIONS | {"themes.manage"}
        with mock.patch.object(views.agent, "run", side_effect=run):
            response = post({"message": "add a View all button"})
        self.assertEqual([a["label"] for a in response.data["actions"]], ["Open product-grid.liquid in Code editor"])

    def test_refuses_when_the_read_only_alias_is_not_configured(self):
        with mock.patch.object(views.queries, "alias_configured", return_value=False), \
                mock.patch.object(views.agent, "run") as run:
            self.assertEqual(post({"message": "hi"}).status_code, 503)
        run.assert_not_called()

    def test_refuses_when_the_session_is_not_read_only(self):
        with mock.patch.object(views.queries, "ensure_read_only", side_effect=queries.ReadOnlyNotEnforced()), \
                mock.patch.object(views.agent, "run") as run:
            self.assertEqual(post({"message": "hi"}).status_code, 503)
        run.assert_not_called()

    def test_a_new_question_replaces_the_old_one_and_the_old_result_is_ignored(self):
        inner = {}

        def first(question, ctx, still_current=None, **kwargs):
            with mock.patch.object(views.agent, "run", return_value=("second answer", [])):
                inner["response"] = post({"message": "again"})  # the person pressed Stop and asked again
            self.assertFalse(still_current())  # the old request sees it was replaced
            return "first answer", []

        with mock.patch.object(views.agent, "run", side_effect=first):
            old = post({"message": "one"})
        self.assertEqual(inner["response"].status_code, 200)  # no 429
        self.assertEqual(inner["response"].data["answer"], "second answer")
        self.assertEqual(old.status_code, 409)  # the old result is dropped
        self.assertNotIn("answer", old.data)
        self.assertIsNone(cache.get("assistant:busy:7"))  # the newer request freed the lock; the old one did not break it

    def test_the_old_request_stops_at_its_next_check_and_never_deletes_the_new_lock(self):
        def first(question, ctx, still_current=None, **kwargs):
            views.take_lock("assistant:busy:7", "newer")  # a newer question took the lock
            if not still_current():
                raise views.agent.Superseded()
            return "late", []

        with mock.patch.object(views.agent, "run", side_effect=first):
            self.assertEqual(post({"message": "one"}).status_code, 409)
        self.assertEqual(cache.get("assistant:busy:7"), "newer")  # still held by the newer question

    def test_the_lock_is_released_after_a_normal_answer(self):
        with mock.patch.object(views.agent, "run", return_value=("ok", [])):
            self.assertEqual(post({"message": "one"}).status_code, 200)
        self.assertIsNone(cache.get("assistant:busy:7"))

    def test_the_lock_helpers(self):
        views.take_lock("k", "a")
        self.assertTrue(views.keep_lock("k", "a"))
        views.take_lock("k", "b")
        self.assertFalse(views.keep_lock("k", "a"))
        views.release_lock("k", "a")  # not its lock: nothing happens
        self.assertEqual(cache.get("k"), "b")
        views.release_lock("k", "b")
        self.assertIsNone(cache.get("k"))

    @override_settings(ASSISTANT_OLLAMA_TIMEOUT=300)
    def test_the_lock_time_is_short_and_renewed_while_the_request_lives(self):
        self.assertEqual(views._lock_ttl(), 315)  # one model reply plus a short grace, not three replies
        with mock.patch.object(views.cache, "set") as put:
            views.take_lock("k", "a")
            put.assert_called_with("k", "a", timeout=315)
        views.take_lock("k", "a")
        with mock.patch.object(views.cache, "set") as put:
            views.keep_lock("k", "a")
            put.assert_called_with("k", "a", timeout=315)

    def test_a_run_replaced_between_rounds_makes_no_further_model_call(self):
        from apps.assistant.tests.test_logic import ctx, scripted, tool_call
        replaced = {"now": False}

        def chat(messages, tools=None):
            replaced["now"] = True  # replaced while this model call runs
            return tool_call("low_stock", {})

        with self.assertRaises(views.agent.Superseded):
            views.agent.run("what is low?", ctx(), chat=chat, still_current=lambda: not replaced["now"])
        replaced["now"] = False
        calls = scripted({"role": "assistant", "content": "ok"})
        self.assertEqual(views.agent.run("hi", ctx(), chat=calls, still_current=lambda: True)[0], "ok")

    def test_the_lock_is_released_when_the_model_fails(self):
        with mock.patch.object(views.agent, "run", side_effect=ollama.OllamaError("Ollama isn't running")):
            response = post({"message": "hi"})
        self.assertEqual(response.status_code, 503)
        self.assertIn("isn't running", response.data["detail"])
        with mock.patch.object(views.agent, "run", return_value=("ok", [])):
            self.assertEqual(post({"message": "hi"}).status_code, 200)

    def test_own_throttle_per_user(self):
        with mock.patch.object(views.agent, "run", return_value=("ok", [])):
            codes = [post({"message": "hi"}).status_code for _ in range(11)]
        self.assertEqual(codes[:10], [200] * 10)
        self.assertEqual(codes[10], 429)


class SwitchedOffTests(SimpleTestCase):
    @override_settings(ASSISTANT_REAL_MODE=False)
    def test_404_without_the_flag(self):
        with mock.patch.object(views.agent, "run") as run:
            response = post({"message": "hi"})
        self.assertEqual(response.status_code, 404)
        run.assert_not_called()

    def test_404_even_for_a_signed_out_request(self):
        request = APIRequestFactory().post(URL, {"message": "hi"}, format="json")
        with override_settings(ASSISTANT_REAL_MODE=False):
            self.assertEqual(views.AssistantChatView.as_view()(request).status_code, 404)

    def test_no_second_connection_without_the_flag(self):
        from django.conf import settings
        self.assertNotIn("assistant_ro", settings.DATABASES)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@override_settings(ASSISTANT_OLLAMA_URL="http://localhost:11434", ASSISTANT_OLLAMA_MODEL="qwen2.5:7b",
                   ASSISTANT_OLLAMA_TIMEOUT=300, ASSISTANT_PROVIDER="ollama")
class OllamaTests(SimpleTestCase):
    def call(self, side_effect=None, body=None):
        opener = mock.Mock()
        if side_effect:
            opener.open.side_effect = side_effect
        else:
            opener.open.return_value = FakeResponse(json.dumps(body).encode())
        with mock.patch.object(ollama, "_opener", opener):
            return ollama.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}]), opener

    def test_posts_to_api_chat_without_streaming(self):
        message, opener = self.call(body={"message": {"role": "assistant", "content": "hello"}})
        self.assertEqual(message["content"], "hello")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://localhost:11434/api/chat")
        payload = json.loads(request.data)
        self.assertIs(payload["stream"], False)
        self.assertEqual(payload["model"], "qwen2.5:7b")
        self.assertEqual(payload["tools"], [{"type": "function"}])
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 300)

    def test_not_running(self):
        with self.assertRaisesMessage(ollama.OllamaError, "isn't running"):
            self.call(side_effect=urllib.error.URLError(ConnectionRefusedError()))

    def test_timeout(self):
        with self.assertRaisesMessage(ollama.OllamaError, "300 seconds"):
            self.call(side_effect=TimeoutError())
        with self.assertRaisesMessage(ollama.OllamaError, "300 seconds"):
            self.call(side_effect=urllib.error.URLError(TimeoutError()))

    def test_model_missing(self):
        error = urllib.error.HTTPError("u", 404, "Not Found", {}, io.BytesIO(b'{"error": "model not found"}'))
        with self.assertRaisesMessage(ollama.OllamaError, "ollama pull qwen2.5:7b"):
            self.call(side_effect=error)

    def test_garbage_reply(self):
        with self.assertRaises(ollama.OllamaError):
            self.call(body={"nope": 1})


class QueriesStaticTests(SimpleTestCase):
    """queries.py is the only file that reads the database; check its rules by reading the code."""

    source = Path(queries.__file__).read_text(encoding="utf-8")

    def test_no_write_calls(self):
        for word in (".save(", ".update(", ".delete(", ".create(", "bulk_", "get_or_create", "update_or_create",
                     ".raw("):
            self.assertNotIn(word, self.source, word)

    def test_every_model_query_uses_the_read_only_alias(self):
        tree = ast.parse(self.source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "objects":
                call = next((p for p in ast.walk(tree) if isinstance(p, ast.Call)
                             and isinstance(p.func, ast.Attribute) and p.func.attr == "using"
                             and p.func.value is node), None)
                self.assertIsNotNone(call, f"line {node.lineno}: .objects without .using(ALIAS)")

    def test_every_query_is_filtered_by_store(self):
        for name in ("_orders", "sales_totals", "low_stock_rows", "orders_page",
                     "_own_themes", "theme_for", "theme_file_list", "theme_file_text"):
            function = next(n for n in ast.parse(self.source).body if isinstance(n, ast.FunctionDef) and n.name == name)
            self.assertIn("store_id", [a.arg for a in function.args.args], name)
