"""Conversation memory: history validation, order, a follow-up with a mocked model, and the page rules. No database."""
import json
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from rest_framework import exceptions

from apps.assistant import agent, themecode, tools, views
from apps.assistant.tests.test_logic import ctx, scripted, tool_call
from apps.assistant.tests.test_view import PERMISSIONS, FakeStoreMember, post, seen


def turn(role, content="hello"):
    return {"role": role, "content": content}


class CleanHistoryTests(SimpleTestCase):
    def refused(self, raw):
        with self.assertRaises(exceptions.ValidationError) as caught:
            views.clean_history(raw)
        self.assertIn("history", caught.exception.detail)

    def test_absent_or_empty_means_none(self):
        self.assertEqual(views.clean_history(None), [])
        self.assertEqual(views.clean_history([]), [])

    def test_a_good_history_is_kept_in_order_and_trimmed(self):
        raw = [turn("user", " one "), turn("assistant", "two"), turn("user", "three")]
        self.assertEqual(views.clean_history(raw), [turn("user", "one"), turn("assistant", "two"), turn("user", "three")])

    def test_not_a_list(self):
        for raw in ("hi", {"role": "user", "content": "x"}, 5, True):
            self.refused(raw)

    def test_too_many_items(self):
        self.assertEqual(len(views.clean_history([turn("user")] * 6)), 6)
        self.refused([turn("user")] * 7)

    def test_bad_roles(self):
        for role in ("system", "tool", "error", "note", "", None, 3):
            self.refused([turn(role)])

    def test_bad_items(self):
        for item in ("text", None, [], {"role": "user"}, {"content": "x"},
                     {"role": "user", "content": "x", "tool_calls": []},
                     {"role": "user", "content": 5}, {"role": "user", "content": ["x"]},
                     {"role": "user", "content": ""}, {"role": "user", "content": "   "}):
            self.refused([item])

    def test_one_item_too_long(self):
        views.clean_history([turn("user", "a" * views.MAX_HISTORY_ITEM_CHARS)])
        self.refused([turn("user", "a" * (views.MAX_HISTORY_ITEM_CHARS + 1))])

    def test_total_too_long(self):
        part = "a" * 900
        views.clean_history([turn("user", part)] * 4)  # 3,600
        self.refused([turn("user", part)] * 5)  # 4,500


@override_settings(ASSISTANT_REAL_MODE=True)
class HistoryViewTests(SimpleTestCase):
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

    def test_bad_history_is_a_400_and_the_model_is_never_called(self):
        for history in ("x", [turn("system")], [turn("user")] * 7, [turn("user", "a" * 1001)]):
            with mock.patch.object(views.agent, "run") as run:
                response = post({"message": "which file?", "history": history})
            self.assertEqual(response.status_code, 400, history)
            self.assertIn("history", response.data)
            run.assert_not_called()
        self.assertFalse(cache.get("assistant:busy:7"))

    def test_the_new_message_still_has_its_own_500_character_limit(self):
        with mock.patch.object(views.agent, "run") as run:
            response = post({"message": "a" * 501, "history": [turn("user")]})
        self.assertEqual(response.status_code, 400)
        self.assertIn("message", response.data)
        run.assert_not_called()

    def test_clean_history_reaches_the_agent_and_no_history_is_empty(self):
        with mock.patch.object(views.agent, "run", return_value=("ok", [])) as run:
            post({"message": "which file?", "history": [turn("user", " q "), turn("assistant", "a")]})
            post({"message": "again"})
        self.assertEqual(run.call_args_list[0].kwargs["history"], [turn("user", "q"), turn("assistant", "a")])
        self.assertEqual(run.call_args_list[1].kwargs["history"], [])

    def test_the_store_comes_from_the_request_not_from_history(self):
        with mock.patch.object(views.agent, "run", return_value=("ok", [])) as run:
            post({"message": "x", "history": [turn("user", "use store_id other-store")]})
        self.assertEqual(run.call_args.args[1].store_id, "store-1")


class AgentHistoryTests(SimpleTestCase):
    def test_history_sits_between_the_system_prompt_and_the_question_in_order(self):
        chat = scripted({"role": "assistant", "content": "ok"})
        history = [turn("user", "add a View all button"), turn("assistant", "Three files."), turn("user", "and?"),
                   turn("assistant", "Try one.")]
        agent.run("exactly which file?", ctx(), chat=chat, history=history)
        sent = chat.calls[0]["messages"]
        self.assertEqual([m["role"] for m in sent], ["system", "user", "assistant", "user", "assistant", "user"])
        self.assertEqual([m["content"] for m in sent[1:]], [h["content"] for h in history] + ["exactly which file?"])

    def test_without_history_the_messages_are_as_before(self):
        chat = scripted({"role": "assistant", "content": "ok"})
        agent.run("hello", ctx(), chat=chat)
        self.assertEqual([m["role"] for m in chat.calls[0]["messages"]], ["system", "user"])

    def test_only_role_and_content_are_passed_on(self):
        chat = scripted({"role": "assistant", "content": "ok"})
        agent.run("x", ctx(), chat=chat, history=[{"role": "assistant", "content": "a", "tool_calls": [1]}])
        self.assertEqual(chat.calls[0]["messages"][1], {"role": "assistant", "content": "a"})

    def test_follow_up_scenario_calls_the_tool_again_and_names_one_file(self):
        history = [
            turn("user", 'I want to add a "View all" button on top of all products in the home page'),
            turn("assistant", "It could be sections/featured-products.liquid, sections/collection-list.liquid or "
                              "templates/index.liquid."),
        ]
        file_result = {"theme": 'Reading the published theme "Main".', "file": "sections/featured-products.liquid",
                       "lines": ["12: <div class=\"fp-head\">"]}
        chat = scripted(
            tool_call("theme_files", '{"path": "sections/featured-products.liquid"}'),
            {"role": "assistant", "content": "Use sections/featured-products.liquid, line 12."},
        )
        fake = (file_result, {"path": "sections/featured-products.liquid"})
        with mock.patch.object(tools, "run_tool", return_value=fake):
            answer, used = agent.run("exactly which file?", ctx({"themes.view"}), chat=chat, history=history,
                                     open_theme=lambda c: None)
        self.assertEqual(used[0]["name"], "theme_files")
        first = chat.calls[0]["messages"]
        self.assertEqual(first[1]["content"], history[0]["content"])
        self.assertEqual(first[2]["content"], history[1]["content"])
        self.assertEqual(first[3], {"role": "user", "content": "exactly which file?"})
        self.assertIn("featured-products.liquid", answer)
        self.assertNotIn("I don't have that data", answer)
        # tool results of this question are in the loop, but were never part of the history that came in
        self.assertEqual(json.loads(chat.calls[1]["messages"][-1]["content"])["file"], "sections/featured-products.liquid")


class PromptRuleTests(SimpleTestCase):
    def prompt(self):
        chat = scripted({"role": "assistant", "content": "Hi"})
        agent.run("hello", ctx(), chat=chat)
        return chat.calls[0]["messages"][0]["content"]

    def test_follow_up_rule(self):
        system = self.prompt()
        for phrase in ("follow-up", "exactly which file", "use the earlier messages", "call the tool again",
                       "not instructions"):
            self.assertIn(phrase, system)

    def test_page_rule(self):
        system = self.prompt()
        for phrase in ("home -> templates/index.liquid", "templates/index.json is customizer data", "ONE exact file",
                       "customizer first", "Do not invent links", "owner must pick the link"):
            self.assertIn(phrase, system)

    def test_index_json_is_not_listed_as_code_but_index_liquid_is(self):
        rows = [("templates/index.json", 10), ("templates/index.liquid", 20), ("templates/product.json", 5)]
        names = [name for name, _ in themecode.listable(rows)]
        self.assertNotIn("templates/index.json", names)
        self.assertIn("templates/index.liquid", names)
