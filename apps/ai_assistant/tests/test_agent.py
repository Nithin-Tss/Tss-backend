"""The agent loop with the LLM mocked: these tests never reach OpenRouter."""
from unittest import mock

import openai
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APITestCase

from apps.ai_assistant import agent, llm
from apps.ai_assistant.models import AiPageDraft
from apps.themes.models import StorePage

from .helpers import TwoStores, call, reply

HERO = {"type": "hero", "settings": {"heading": "Hello"}}


class AgentLoopTest(TwoStores, APITestCase):
    def setUp(self):
        self.make_stores()
        self.ctx_a = self.ctx(self.store_a)

    def run_agent(self, script, message="build an about page"):
        with mock.patch.object(llm, "chat", side_effect=script) as chat:
            text = agent.run(self.ctx_a, [], message)
        return text, chat

    def test_tool_call_then_final_reply(self):
        text, chat = self.run_agent([
            reply(calls=[call("create_page", {"title": "About", "sections": [HERO]})]),
            reply("I created a draft About page."),
        ])
        self.assertEqual(text, "I created a draft About page.")
        self.assertEqual(chat.call_count, 2)
        self.assertEqual(StorePage.objects.get().status, StorePage.HIDDEN)
        self.assertEqual(self.ctx_a.changes[0]["action"], "created_page")
        # the tool result went back to the model, with the system prompt and tools on every call
        second = chat.call_args_list[1].args[0]
        self.assertEqual(second[-1]["role"], "tool")
        self.assertEqual(second[0]["role"], "system")
        self.assertTrue(chat.call_args_list[0].args[1])

    def test_validation_error_is_sent_back_and_the_model_can_retry(self):
        text, chat = self.run_agent([
            reply(calls=[call("create_page", {"title": "A", "sections": [{"type": "nonsense"}]}, "c1")]),
            reply(calls=[call("create_page", {"title": "A", "sections": [HERO]}, "c2")]),
            reply("Done."),
        ])
        fed_back = next(m for m in chat.call_args_list[1].args[0] if m.get("tool_call_id") == "c1")
        self.assertIn("Unknown section type", fed_back["content"])
        self.assertEqual(text, "Done.")
        self.assertEqual(StorePage.objects.count(), 1)

    def test_malformed_json_arguments_are_reported_to_the_model(self):
        text, chat = self.run_agent([
            reply(calls=[call("create_page", "{oops")]),
            reply("Sorry, retrying later."),
        ])
        self.assertIn("not valid JSON", chat.call_args_list[1].args[0][-1]["content"])

    def test_gives_up_after_three_retries(self):
        bad = lambda i: reply(calls=[call("create_page", {"title": "A", "sections": [{"type": "nope"}]}, f"c{i}")])
        text, chat = self.run_agent([bad(i) for i in range(10)])
        self.assertEqual(chat.call_count, agent.MAX_RETRIES + 1)
        self.assertIn("couldn't get that to work", text)
        self.assertFalse(StorePage.objects.exists())

    def test_iteration_cap(self):
        ok = lambda i: reply(calls=[call("list_pages", {}, f"c{i}")])
        text, chat = self.run_agent([ok(i) for i in range(20)])
        self.assertEqual(chat.call_count, agent.MAX_ITERATIONS)
        self.assertIn("more steps", text)

    def test_a_clarifying_question_is_just_text(self):
        text, chat = self.run_agent([reply("What should the page be about?")], "make a page")
        self.assertEqual(text, "What should the page be about?")
        self.assertEqual(self.ctx_a.changes, [])

    def test_history_is_sent_before_the_new_message(self):
        with mock.patch.object(llm, "chat", return_value=reply("ok")) as chat:
            agent.run(self.ctx_a, [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "sure"}], "now")
        sent = chat.call_args.args[0]
        self.assertEqual([m["content"] for m in sent[1:]], ["earlier", "sure", "now"])

    def test_model_error_with_no_changes_is_raised(self):
        with mock.patch.object(llm, "chat", side_effect=llm.LLMError("busy")):
            with self.assertRaises(llm.LLMError):
                agent.run(self.ctx_a, [], "hi")

    def test_model_error_after_changes_keeps_the_changes(self):
        text, _ = self.run_agent([
            reply(calls=[call("create_page", {"title": "A", "sections": [HERO]})]),
            llm.LLMError("The AI service is busy right now."),
        ])
        self.assertIn("busy", text)
        self.assertEqual(len(self.ctx_a.changes), 1)

    def test_failures_are_logged_with_the_model_name_but_no_content(self):
        with override_settings(OPENROUTER_MODEL="vendor/some-model"):
            with self.assertLogs("apps.ai_assistant.agent", level="WARNING") as logs:
                self.run_agent([
                    reply(calls=[call("create_page", {"title": "SECRET TITLE", "sections": [{"type": "nope"}]})]),
                    reply("x"),
                ])
        joined = "\n".join(logs.output)
        self.assertIn("model=vendor/some-model", joined)
        self.assertIn("failure=validation", joined)
        self.assertNotIn("SECRET TITLE", joined)

    def test_model_cannot_escape_its_store(self):
        other = self.ctx(self.store_b)
        with mock.patch.object(llm, "chat", side_effect=[reply(calls=[call("create_page", {"title": "B", "sections": [HERO]})]), reply("ok")]):
            agent.run(other, [], "go")
        page_id = other.changes[0]["page_id"]
        text, chat = self.run_agent([reply(calls=[call("get_page", {"page_id": page_id})]), reply("no")])
        self.assertIn("No such page", chat.call_args_list[1].args[0][-1]["content"])


class LlmClientTest(SimpleTestCase):
    def _error(self, cls, status):
        response = mock.MagicMock(status_code=status, headers={}, request=mock.MagicMock())
        return cls("boom", response=response, body=None)

    @override_settings(OPENROUTER_API_KEY="test-key-not-real")
    def test_api_errors_become_clean_messages(self):
        cases = [
            (self._error(openai.RateLimitError, 429), "busy"),
            (self._error(openai.AuthenticationError, 401), "isn't set up correctly"),
            (self._error(openai.InternalServerError, 500), "returned an error"),
            (openai.APITimeoutError(request=mock.MagicMock()), "too long"),
            (openai.APIConnectionError(request=mock.MagicMock()), "reach"),
        ]
        for error, expected in cases:
            client = mock.MagicMock()
            client.chat.completions.create.side_effect = error
            with mock.patch.object(llm, "_client", return_value=client):
                with self.assertRaises(llm.LLMError) as caught:
                    llm.chat([{"role": "user", "content": "hi"}])
            self.assertIn(expected, str(caught.exception))
            self.assertNotIn("test-key-not-real", str(caught.exception))

    @override_settings(OPENROUTER_API_KEY="")
    def test_missing_key_is_a_clean_error(self):
        with self.assertRaisesMessage(llm.LLMError, "isn't set up"):
            llm.chat([])

    @override_settings(OPENROUTER_API_KEY="k", OPENROUTER_MODEL="vendor/paid-model", OPENROUTER_BASE_URL="https://openrouter.ai/api/v1")
    def test_model_and_base_url_come_from_settings(self):
        with mock.patch.object(openai, "OpenAI") as ctor:
            ctor.return_value.chat.completions.create.return_value = mock.MagicMock(choices=[mock.MagicMock(message="m")])
            self.assertEqual(llm.chat([{"role": "user", "content": "x"}]), "m")
        self.assertEqual(ctor.call_args.kwargs["base_url"], "https://openrouter.ai/api/v1")
        self.assertEqual(ctor.return_value.chat.completions.create.call_args.kwargs["model"], "vendor/paid-model")

    @override_settings(OPENROUTER_API_KEY="k")
    def test_empty_choices_is_a_clean_error(self):
        with mock.patch.object(llm, "_client") as client:
            client.return_value.chat.completions.create.return_value = mock.MagicMock(choices=[])
            with self.assertRaisesMessage(llm.LLMError, "empty answer"):
                llm.chat([])
