"""
The help assistant and the support-ticket flows, with the LLM always mocked (these tests never reach OpenRouter).

    python manage.py test apps.helpdesk --settings=config.test_settings
"""
import json
from unittest import mock

from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase

from apps.ai_assistant import llm
from apps.ai_assistant.models import Conversation, Message
from apps.ai_assistant.tests.helpers import TwoStores, call, reply
from apps.delivery.models import DeliveryGateway
from apps.helpdesk import escalation, help_agent, help_tools, search, services
from apps.helpdesk.models import HelpArticle, Ticket, TicketMessage
from apps.identity.models import User
from apps.platform_admin.models import FeatureFlag, FeatureFlagOverride
from apps.tenancy.models import ShippingRate, ShippingZone, StoreDomain

CHAT = "/api/v1/help-assistant/chat/"
STAFF = "/api/v1/platform/support/tickets/"
MINE = "/api/v1/support/tickets/"
SECRET = "sk_live_TOPSECRET_123"


def escalate_url(conversation_id):
    return f"/api/v1/help-assistant/conversations/{conversation_id}/escalate/"


def enable(store):
    flag, _ = FeatureFlag.objects.get_or_create(key="help_assistant", defaults={"name": "Help assistant"})
    FeatureFlagOverride.objects.update_or_create(flag=flag, store_id=store.pk, defaults={"enabled": True})


def raise_call(**extra):
    args = {"subject": "Domain will not connect", "summary": "Followed the DNS steps; still failing.",
            "category": "domains", "priority": "high", **extra}
    return call("raise_support_ticket", args)


class Base(TwoStores, APITestCase):
    def setUp(self):
        cache.clear()
        self.make_stores()
        enable(self.store_a)
        enable(self.store_b)
        self.user_a = User.objects.get(email="a@example.com")
        self.user_b = User.objects.get(email="b@example.com")
        HelpArticle.objects.create(
            slug="connect-domain", title="Connect a custom domain", category="domains", is_published=True,
            body="Open Settings > Domains and add your domain.", keywords="domain, dns",
        )
        self.as_(self.a)

    def chat(self, script, **body):
        with mock.patch.object(llm, "chat", side_effect=script) as chat:
            response = self.client.post(CHAT, body, format="json")
        return response, chat

    def conversation(self, user=None, store=None, escalated=False):
        return Conversation.objects.create(
            store=store or self.store_a, user=user or self.user_a, assistant_type=Conversation.HELP, escalated=escalated,
        )

    def say(self, conversation, text):
        Message.objects.create(conversation=conversation, role="user", content=text)

    def make_staff(self, email="agent@example.com", **flags):
        token, _ = self.sign_up(email, "Staff store")
        User.objects.filter(email=email).update(**flags)
        return token

    def as_staff(self, token):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")


class AgentLoopTests(Base):
    def test_search_then_answer_uses_the_help_profile_and_returns_sources(self):
        with override_settings(HELP_ASSISTANT_MODEL="vendor/help-model"):
            r, chat = self.chat(
                [reply(calls=[call("search_help_articles", {"query": "connect my domain"})]),
                 reply("Open Settings > Domains. Did that solve it?")],
                message="How do I connect my domain?",
            )
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(body["sources"], [
            {"title": "Connect a custom domain", "slug": "connect-domain", "url": "/admin/help/articles/connect-domain"},
        ])
        self.assertFalse(body["escalated"])
        first = chat.call_args_list[0]
        self.assertIn("support guide", first.args[0][0]["content"])           # the help prompt, not the page builder's
        self.assertEqual(first.kwargs["model"], "vendor/help-model")          # HELP_ASSISTANT_MODEL
        self.assertEqual({t["function"]["name"] for t in first.args[1]}, set(help_tools.TOOLS))
        tool_message = chat.call_args_list[1].args[0][-1]
        self.assertEqual(tool_message["role"], "tool")
        self.assertIn("/admin/help/articles/connect-domain", tool_message["content"])
        self.assertEqual(list(Message.objects.values_list("role", flat=True)), ["user", "assistant"])

    def test_help_model_falls_back_to_openrouter_model(self):
        with override_settings(HELP_ASSISTANT_MODEL="", OPENROUTER_MODEL="vendor/default"):
            r, chat = self.chat([reply("Hi!")], message="hello")
        self.assertEqual(chat.call_args.kwargs["model"], "vendor/default")

    def test_unknown_tool_and_bad_arguments_are_sent_back_to_the_model(self):
        r, chat = self.chat(
            [reply(calls=[call("delete_everything", {}, "c1"), call("search_help_articles", "{not json", "c2")]),
             reply("Sorry, let me help differently.")], message="hi",
        )
        fed_back = {m["tool_call_id"]: m["content"] for m in chat.call_args_list[1].args[0] if m["role"] == "tool"}
        self.assertIn("Unknown tool", fed_back["c1"])
        self.assertIn("not valid JSON", fed_back["c2"])
        self.assertEqual(Ticket.objects.count(), 0)

    def test_model_unreachable_is_a_503_and_stores_nothing(self):
        r, _ = self.chat(llm.LLMError("busy"), message="hello")
        self.assertEqual((r.status_code, r.json()["detail"]), (503, "busy"))
        self.assertEqual(Message.objects.count(), 0)


class RaiseTicketToolTests(Base):
    def test_the_model_raises_a_ticket_and_the_merchant_gets_the_number(self):
        r, _ = self.chat([reply(calls=[raise_call()]), reply("I've raised a ticket for you.")], message="my domain is broken")
        body = r.json()
        ticket = Ticket.objects.get()
        self.assertEqual(ticket.ticket_number, "TSS-000001")
        self.assertEqual((ticket.source, ticket.status, ticket.category, ticket.priority),
                         ("ai_escalation", "open", "domains", "high"))
        self.assertEqual((ticket.store_id, ticket.user_id), (self.store_a.pk, self.user_a.pk))
        self.assertEqual(ticket.subject, "Domain will not connect")
        self.assertIn("Followed the DNS steps", ticket.summary)
        self.assertEqual(ticket.conversation_id.hex, body["conversation_id"].replace("-", ""))
        self.assertTrue(Conversation.objects.get().escalated)
        # the model forgot the number: the server adds it
        self.assertIn("TSS-000001", body["reply"])
        self.assertEqual(body["ticket"]["ticketNumber"], "TSS-000001")
        self.assertTrue(body["escalated"])

    def test_after_a_ticket_the_ai_stops_answering(self):
        r, _ = self.chat([reply(calls=[raise_call()]), reply("Raised TSS-000001.")], message="help")
        cid = r.json()["conversation_id"]
        r2, chat = self.chat([reply("should never be used")], message="hello again?", conversation_id=cid)
        chat.assert_not_called()
        self.assertEqual(r2.json()["reply"], "A support agent will reply here.")
        self.assertTrue(r2.json()["escalated"])
        self.assertEqual(Message.objects.count(), 2)  # nothing added to the AI chat

    def test_tool_arguments_are_validated(self):
        r, chat = self.chat(
            [reply(calls=[raise_call(category="hacking")]), reply("ok")], message="x")
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertIn("'category' must be one of", chat.call_args_list[1].args[0][-1]["content"])

    def test_raising_twice_returns_the_same_ticket(self):
        self.chat([reply(calls=[raise_call(), raise_call(subject="Again")]), reply("done")], message="x")
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertEqual(Ticket.objects.get().subject, "Domain will not connect")

    def test_a_resolved_ticket_does_not_block_a_new_one_for_a_new_conversation(self):
        self.chat([reply(calls=[raise_call()]), reply("ok")], message="a")
        Ticket.objects.update(status="resolved")
        self.chat([reply(calls=[raise_call()]), reply("ok")], message="b")  # a NEW conversation
        self.assertEqual(Ticket.objects.count(), 2)
        self.assertEqual(sorted(Ticket.objects.values_list("ticket_number", flat=True)), ["TSS-000001", "TSS-000002"])


class TalkToAHumanTests(Base):
    def test_the_button_creates_a_ticket_from_the_conversation(self):
        conv = self.conversation()
        self.say(conv, "My payouts are missing")
        summary = json.dumps({"subject": "Payouts missing", "summary": "Merchant reports missing payouts.",
                              "category": "payments", "priority": "urgent"})
        with mock.patch.object(llm, "chat", return_value=reply(summary)) as chat:
            r = self.client.post(escalate_url(conv.pk), {}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        chat.assert_called_once()  # the LLM is used exactly once
        ticket = Ticket.objects.get()
        self.assertEqual((ticket.source, ticket.subject, ticket.category, ticket.priority),
                         ("customer_request", "Payouts missing", "payments", "urgent"))
        self.assertEqual(ticket.conversation_id, conv.pk)
        self.assertEqual(r.json()["ticket"]["ticketNumber"], ticket.ticket_number)
        conv.refresh_from_db()
        self.assertTrue(conv.escalated)

    def test_the_button_still_creates_a_ticket_when_the_llm_fails(self):
        for failure in (llm.LLMError("down"), RuntimeError("boom"), ValueError("x")):
            conv = self.conversation()
            self.say(conv, "first question")
            self.say(conv, "Where is my money?")
            with mock.patch.object(llm, "chat", side_effect=failure):
                r = self.client.post(escalate_url(conv.pk), {}, format="json")
            self.assertEqual(r.status_code, 201, r.content)
            ticket = Ticket.objects.get(conversation=conv)
            self.assertEqual(ticket.summary, "Where is my money?")
            self.assertIn("Where is my money?", ticket.subject)
            self.assertEqual((ticket.source, ticket.category, ticket.priority), ("customer_request", "other", "normal"))

    def test_garbage_from_the_llm_falls_back_too(self):
        for text in ("I am not JSON", "{}", '{"subject": "", "summary": ""}', None):
            conv = self.conversation()
            self.say(conv, "Need help with my theme")
            with mock.patch.object(llm, "chat", return_value=reply(text)):
                r = self.client.post(escalate_url(conv.pk), {}, format="json")
            self.assertEqual(r.status_code, 201)
            self.assertEqual(Ticket.objects.get(conversation=conv).summary, "Need help with my theme")

    def test_no_api_key_configured_still_creates_the_ticket(self):
        conv = self.conversation()
        self.say(conv, "help me")
        with override_settings(OPENROUTER_API_KEY=""):
            r = self.client.post(escalate_url(conv.pk), {}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(Ticket.objects.get().summary, "help me")

    def test_clicking_before_any_message_works(self):
        with mock.patch.object(llm, "chat") as chat:
            r = self.client.post(escalate_url("new"), {"message": "I need a person"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        ticket = Ticket.objects.get()
        self.assertEqual(ticket.summary, "I need a person")  # no chat yet: the merchant's own note, no model needed
        chat.assert_not_called()

    def test_a_malformed_llm_category_is_replaced_by_other(self):
        conv = self.conversation()
        self.say(conv, "x")
        bad = json.dumps({"subject": "S", "summary": "T", "category": "nonsense", "priority": "apocalyptic"})
        with mock.patch.object(llm, "chat", return_value=reply(bad)):
            self.client.post(escalate_url(conv.pk), {}, format="json")
        self.assertEqual(Ticket.objects.get().category, "other")
        self.assertEqual(Ticket.objects.get().priority, "normal")

    def test_clicking_twice_returns_the_existing_open_ticket(self):
        conv = self.conversation()
        self.say(conv, "help")
        with mock.patch.object(llm, "chat", side_effect=llm.LLMError("x")) as chat:
            first = self.client.post(escalate_url(conv.pk), {}, format="json")
            calls_after_first = chat.call_count
            second = self.client.post(escalate_url(conv.pk), {}, format="json")
        self.assertEqual((first.status_code, second.status_code), (201, 200))
        self.assertEqual(first.json()["ticket"]["id"], second.json()["ticket"]["id"])
        self.assertFalse(second.json()["created"])
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertEqual(chat.call_count, calls_after_first)  # the duplicate did not even call the model

    def test_tool_then_button_and_button_then_tool_share_one_ticket(self):
        r, _ = self.chat([reply(calls=[raise_call()]), reply("ok")], message="x")
        cid = r.json()["conversation_id"]
        again = self.client.post(escalate_url(cid), {}, format="json")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(Ticket.objects.count(), 1)
        # button first, then the tool, directly
        conv = self.conversation()
        ctx = help_tools.HelpContext(store=self.store_a, user=self.user_a, conversation=conv)
        with mock.patch.object(llm, "chat", side_effect=llm.LLMError("x")):
            self.client.post(escalate_url(conv.pk), {}, format="json")
        result, kind = help_tools.run_tool(ctx, "raise_support_ticket", json.dumps(
            {"subject": "s", "summary": "t", "category": "other"}))
        self.assertIsNone(kind)
        self.assertTrue(result["already_existed"])
        self.assertEqual(Ticket.objects.filter(conversation=conv).count(), 1)

    def test_cannot_escalate_someone_elses_conversation(self):
        theirs = self.conversation(user=self.user_b, store=self.store_b)
        r = self.client.post(escalate_url(theirs.pk), {}, format="json")
        self.assertEqual(r.status_code, 404)
        also_mine_but_page_builder = Conversation.objects.create(store=self.store_a, user=self.user_a)
        self.assertEqual(self.client.post(escalate_url(also_mine_but_page_builder.pk), {}, format="json").status_code, 404)
        self.assertEqual(Ticket.objects.count(), 0)


class AccessTests(Base):
    def test_off_by_default_403(self):
        FeatureFlagOverride.objects.all().delete()
        self.assertEqual(self.client.post(CHAT, {"message": "hi"}, format="json").status_code, 403)
        self.assertEqual(self.client.post(escalate_url("new"), {}, format="json").status_code, 403)

    def test_signed_out_is_refused(self):
        self.client.credentials()
        r = self.client.post(CHAT, {"message": "hi"}, format="json")
        self.assertIn(r.status_code, (400, 401, 403), r.content)
        self.assertEqual((Conversation.objects.count(), Message.objects.count()), (0, 0))

    def test_throttled_per_store(self):
        from apps.helpdesk.help_views import StoreThrottle

        with mock.patch.object(StoreThrottle, "get_rate", return_value="2/hour"):
            codes = [self.chat([reply("hi")], message="hi")[0].status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])

    def test_message_validation(self):
        self.assertEqual(self.client.post(CHAT, {"message": "  "}, format="json").status_code, 400)
        self.assertEqual(self.client.post(CHAT, {"message": "x" * 2001}, format="json").status_code, 400)

    def test_chats_are_separate_from_the_page_builder(self):
        r, _ = self.chat([reply("hi")], message="hello")
        cid = r.json()["conversation_id"]
        self.assertEqual(Conversation.objects.get().assistant_type, "help")
        from apps.ai_assistant.views import ChatView as PageBuilderChat  # noqa: F401

        # the page builder cannot continue a help chat (and the other way round)
        FeatureFlag.objects.get_or_create(key="ai_page_builder", defaults={"name": "x"})
        flag = FeatureFlag.objects.get(key="ai_page_builder")
        FeatureFlagOverride.objects.create(flag=flag, store_id=self.store_a.pk, enabled=True)
        with mock.patch.object(llm, "chat"):
            r = self.client.post("/api/v1/ai-assistant/chat/", {"message": "hi", "conversation_id": cid}, format="json")
        self.assertEqual(r.status_code, 404)
        page_builder = Conversation.objects.create(store=self.store_a, user=self.user_a)
        self.assertEqual(self.client.post(CHAT, {"message": "hi", "conversation_id": str(page_builder.pk)}, format="json").status_code, 404)

    def test_other_merchants_conversation_is_404(self):
        theirs = self.conversation(user=self.user_b, store=self.store_b)
        r = self.client.post(CHAT, {"message": "hi", "conversation_id": str(theirs.pk)}, format="json")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/help-assistant/conversations/{theirs.pk}/").status_code, 404)

    def test_articles_endpoint_hides_unpublished(self):
        HelpArticle.objects.create(slug="secret-draft", title="Draft", body="x", is_published=False)
        self.assertEqual(self.client.get("/api/v1/help-assistant/articles/connect-domain/").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/help-assistant/articles/secret-draft/").status_code, 404)


class ToolSafetyTests(Base):
    def setUp(self):
        super().setUp()
        Store = type(self.store_a)
        Store.objects.filter(pk=self.store_a.pk).update(
            stripe_account_id="acct_SECRET", stripe_publishable_key="pk_live_SECRET",
            stripe_secret_key_enc=SECRET, stripe_webhook_secret_enc="whsec_SECRET", stripe_webhook_endpoint_id="we_SECRET",
            stripe_charges_enabled=True, contact_email="owner-a@example.com", phone="+910000000000",
        )
        Store.objects.filter(pk=self.store_b.pk).update(stripe_secret_key_enc="sk_B_SECRET", currency="USD")
        zone_b = ShippingZone.objects.create(store=self.store_b, name="B-only zone")
        ShippingRate.objects.create(store=self.store_b, zone=zone_b, name="B rate", price=99)
        zone_a = ShippingZone.objects.create(store=self.store_a, name="A zone")
        ShippingRate.objects.create(store=self.store_a, zone=zone_a, name="A rate", price=5)
        StoreDomain.objects.create(store=self.store_a, host="shop-a.example.com")
        StoreDomain.objects.create(store=self.store_b, host="shop-b.example.com")
        DeliveryGateway.objects.create(store=self.store_a, name="Courier", code="c", credentials_enc="CRED_SECRET",
                                       webhook_secret_enc="HOOK_SECRET", is_active=True, is_deleted=False)
        self.store_a.refresh_from_db()
        self.ctx = help_tools.HelpContext(store=self.store_a, user=self.user_a, conversation=self.conversation())

    def run_tool(self, name, **args):
        result, kind = help_tools.run_tool(self.ctx, name, json.dumps(args))
        self.assertIsNone(kind, result)
        return result

    def test_store_settings_never_contain_secrets_or_other_stores(self):
        result = self.run_tool("get_store_settings")
        blob = json.dumps(result).lower()
        for forbidden in ("secret", "acct_", "pk_live", "whsec", "we_", "cred", "owner-a@example.com", "+9100",
                          "b-only", "shop-b", "usd"):
            self.assertNotIn(forbidden.lower(), blob, forbidden)

        def keys(value):
            if isinstance(value, dict):
                for k, v in value.items():
                    yield k
                    yield from keys(v)
            elif isinstance(value, list):
                for v in value:
                    yield from keys(v)

        for key in keys(result):
            for bad in ("secret", "token", "password", "_enc", "api_key", "credential", "webhook"):
                self.assertNotIn(bad, key.lower())
        self.assertTrue(result["payments"]["stripe_connected"])
        self.assertEqual(result["domains"], [{"host": "shop-a.example.com", "kind": "storefront"}])
        self.assertEqual(result["shipping"]["zones"][0]["name"], "A zone")

    def test_only_whitelisted_top_level_sections(self):
        self.assertEqual(set(self.run_tool("get_store_settings")), {
            "general", "taxes", "shipping", "payments", "checkout", "returns_and_cancellations", "domains"})

    def test_tools_ignore_store_and_user_arguments_from_the_model(self):
        result, kind = help_tools.run_tool(self.ctx, "get_store_settings", json.dumps(
            {"store_id": str(self.store_b.pk), "store": "b"}))
        self.assertEqual(result["general"]["store_name"], "Store A")

    def test_my_tickets_are_only_mine(self):
        mine = Ticket.objects.create(store=self.store_a, user=self.user_a, subject="Mine")
        Ticket.objects.create(store=self.store_b, user=self.user_b, subject="Theirs")
        Ticket.objects.create(store=self.store_a, user=None, subject="Orphan")
        result = self.run_tool("get_my_tickets")
        self.assertEqual([t["subject"] for t in result["tickets"]], ["Mine"])
        self.assertEqual(result["tickets"][0]["ticket_number"], mine.ticket_number)

    def test_search_only_returns_published_articles(self):
        HelpArticle.objects.create(slug="d", title="Domain draft", body="domain secrets", keywords="domain", is_published=False)
        result = self.run_tool("search_help_articles", query="domain")
        self.assertEqual([a["title"] for a in result["articles"]], ["Connect a custom domain"])
        self.assertEqual(self.run_tool("search_help_articles", query="zzzz")["articles"], [])

    def test_raise_ticket_is_the_only_writer_and_only_creates_tickets(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as queries:
            for name in ("search_help_articles", "get_store_settings", "get_my_tickets"):
                help_tools.run_tool(self.ctx, name, json.dumps({"query": "domain"}))
        writes = [q["sql"] for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
        self.assertEqual(writes, [])
        self.assertEqual(set(help_tools.TOOLS), {"search_help_articles", "get_store_settings", "get_my_tickets", "raise_support_ticket"})


class MerchantTicketTests(Base):
    def setUp(self):
        super().setUp()
        self.ticket = Ticket.objects.create(
            store=self.store_a, user=self.user_a, subject="Mine", conversation=self.conversation(), summary="S")
        self.say(self.ticket.conversation, "hello there")
        TicketMessage.objects.create(ticket=self.ticket, sender_type="staff", sender=None, body="Public reply")
        TicketMessage.objects.create(ticket=self.ticket, sender_type="staff", sender=None, body="INTERNAL: refund risk",
                                     is_internal=True)

    def test_detail_has_transcript_and_hides_internal_notes(self):
        data = self.ok("get", f"{MINE}{self.ticket.id}/")
        self.assertEqual(data["ticketNumber"], self.ticket.ticket_number)
        self.assertEqual([t["content"] for t in data["transcript"]], ["hello there"])
        self.assertEqual([m["body"] for m in data["messages"]], ["Public reply"])
        self.assertNotIn("INTERNAL", json.dumps(data))
        self.assertNotIn("isInternal", json.dumps(data))
        listing = self.ok("get", MINE)["results"][0]
        self.assertEqual(listing["messageCount"], 1)  # notes are not counted for the merchant

    def test_cannot_see_or_reply_to_another_merchants_ticket(self):
        theirs = Ticket.objects.create(store=self.store_b, user=self.user_b, subject="Theirs")
        self.assertEqual(self.client.get(f"{MINE}{theirs.id}/").status_code, 404)
        self.assertEqual(self.client.post(f"{MINE}{theirs.id}/messages/", {"message": "hi"}, format="json").status_code, 404)
        self.assertEqual([t["subject"] for t in self.ok("get", MINE)["results"]], ["Mine"])
        self.assertEqual(theirs.messages.count(), 0)

    def test_reply_reopens_a_waiting_ticket(self):
        Ticket.objects.filter(pk=self.ticket.pk).update(status="waiting_on_customer")
        self.ok("post", f"{MINE}{self.ticket.id}/messages/", {"message": "Here is more"}, expect=201)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, "open")


class StaffAccessTests(Base):
    def setUp(self):
        super().setUp()
        self.ticket = Ticket.objects.create(store=self.store_a, user=self.user_a, subject="Mine")

    def test_merchants_cannot_use_any_staff_endpoint(self):
        t = self.ticket.id
        for method, url, body in [
            ("get", STAFF, None), ("get", f"{STAFF}{t}/", None), ("patch", f"{STAFF}{t}/", {"status": "closed"}),
            ("post", f"{STAFF}{t}/messages/", {"message": "x"}),
            ("post", f"{STAFF}{t}/messages/", {"message": "x", "internal": True}),
            ("get", "/api/v1/platform/support/summary/", None), ("get", "/api/v1/platform/support/agents/", None),
        ]:
            r = getattr(self.client, method)(url, body, format="json")
            self.assertEqual(r.status_code, 403, f"{method} {url}")
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, "open")
        self.assertEqual(self.ticket.messages.count(), 0)

    def test_signed_out_is_refused(self):
        self.client.credentials()
        self.assertIn(self.client.get(STAFF).status_code, (401, 403))

    def test_support_agents_and_platform_admins_get_in(self):
        agent = self.make_staff("agent@example.com", is_support_agent=True)
        self.as_staff(agent)
        self.assertEqual(self.client.get(STAFF).status_code, 200)
        admin = self.make_staff("root@example.com", is_owner=True)
        self.as_staff(admin)
        self.assertEqual(self.client.get(STAFF).status_code, 200)

    def test_a_support_agent_is_not_a_platform_admin(self):
        self.as_staff(self.make_staff("agent@example.com", is_support_agent=True))
        self.assertEqual(self.client.get("/api/v1/platform/overview/").status_code, 403)
        self.assertEqual(self.client.get("/api/v1/platform/users/").status_code, 403)

    def test_only_platform_admins_toggle_support_agent(self):
        target = User.objects.get(email="b@example.com")
        url = f"/api/v1/platform/users/{target.pk}/support-agent/"
        self.assertEqual(self.client.post(url, {"isSupportAgent": True}, format="json").status_code, 403)  # merchant
        self.as_staff(self.make_staff("agent@example.com", is_support_agent=True))
        self.assertEqual(self.client.post(url, {"isSupportAgent": True}, format="json").status_code, 403)  # agent
        self.as_staff(self.make_staff("root@example.com", is_owner=True))
        r = self.client.post(url, {"isSupportAgent": True}, format="json")
        self.assertEqual((r.status_code, r.json()["isSupportAgent"]), (200, True))
        target.refresh_from_db()
        self.assertTrue(target.is_support_agent)
        self.assertEqual(self.client.post(url, {"isSupportAgent": "yes"}, format="json").status_code, 400)

    def test_session_payload_says_whether_the_user_is_support_staff(self):
        token = self.make_staff("agent@example.com", is_support_agent=True)
        self.as_staff(token)
        from apps.identity.views import session_payload

        self.assertTrue(session_payload(User.objects.get(email="agent@example.com"))["user"]["isSupportAgent"])
        self.assertFalse(session_payload(self.user_a)["user"]["isSupportAgent"])


class StaffWorkflowTests(Base):
    def setUp(self):
        super().setUp()
        self.token = self.make_staff("agent@example.com", is_support_agent=True)
        self.agent = User.objects.get(email="agent@example.com")
        self.t_low = Ticket.objects.create(store=self.store_a, user=self.user_a, subject="Low one", priority="low")
        self.t_urgent = Ticket.objects.create(store=self.store_b, user=self.user_b, subject="Urgent one", priority="urgent",
                                              category="payments", source="ai_escalation")
        self.t_done = Ticket.objects.create(store=self.store_a, user=self.user_a, subject="Done", status="closed")
        self.as_staff(self.token)

    def subjects(self, query=""):
        return [t["subject"] for t in self.ok("get", f"{STAFF}{query}")["results"]]

    def test_default_order_is_most_urgent_first(self):
        self.assertEqual(self.subjects("?status=active"), ["Urgent one", "Low one"])

    def test_filters_and_search(self):
        self.assertEqual(self.subjects("?status=closed"), ["Done"])
        self.assertEqual(self.subjects("?priority=urgent"), ["Urgent one"])
        self.assertEqual(self.subjects("?category=payments"), ["Urgent one"])
        self.assertEqual(self.subjects("?source=ai_escalation"), ["Urgent one"])
        self.assertEqual(self.subjects("?q=Store B"), ["Urgent one"])
        self.assertEqual(self.subjects(f"?q={self.t_low.ticket_number}"), ["Low one"])
        self.assertEqual(self.subjects("?assigned=unassigned&status=active"), ["Urgent one", "Low one"])
        self.assertEqual(self.subjects("?assigned=me"), [])
        self.assertEqual(self.subjects("?assigned=not-a-uuid"), [])

    def test_assign_changes_status_and_filters_by_me(self):
        r = self.ok("patch", f"{STAFF}{self.t_low.id}/", {"assignedTo": str(self.agent.pk)})
        self.assertEqual((r["assignedTo"]["id"], r["status"]), (str(self.agent.pk), "in_progress"))
        self.assertEqual(self.subjects("?assigned=me"), ["Low one"])
        self.assertEqual(self.ok("get", "/api/v1/platform/support/summary/")["openUnassigned"], 1)  # only "Urgent one"
        r = self.ok("patch", f"{STAFF}{self.t_low.id}/", {"assignedTo": None})
        self.assertIsNone(r["assignedTo"])

    def test_cannot_assign_to_a_merchant(self):
        r = self.client.patch(f"{STAFF}{self.t_low.id}/", {"assignedTo": str(self.user_b.pk)}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.patch(f"{STAFF}{self.t_low.id}/", {"assignedTo": "nope"}, format="json").status_code, 400)

    def test_status_priority_category_and_resolved_at(self):
        r = self.ok("patch", f"{STAFF}{self.t_low.id}/", {"status": "resolved", "priority": "high", "category": "themes"})
        self.assertEqual((r["status"], r["priority"], r["category"]), ("resolved", "high", "themes"))
        self.assertIsNotNone(r["resolvedAt"])
        r = self.ok("patch", f"{STAFF}{self.t_low.id}/", {"status": "open"})
        self.assertIsNone(r["resolvedAt"])
        for bad in ({"priority": "x"}, {"category": "x"}, {"status": "pending"}):
            self.assertEqual(self.client.patch(f"{STAFF}{self.t_low.id}/", bad, format="json").status_code, 400)

    def test_internal_note_is_staff_only_and_does_not_email_or_change_status(self):
        with override_settings(SUPPORT_EMAIL="support@example.com"), \
                mock.patch.object(services, "send_store_email") as send:
            self.ok("post", f"{STAFF}{self.t_low.id}/messages/", {"message": "Looks like fraud", "internal": True}, expect=201)
        send.assert_not_called()
        self.t_low.refresh_from_db()
        self.assertEqual(self.t_low.status, "open")
        staff_view = self.ok("get", f"{STAFF}{self.t_low.id}/")
        self.assertEqual([(m["body"], m["isInternal"]) for m in staff_view["messages"]], [("Looks like fraud", True)])
        self.as_(self.a)
        self.assertNotIn("fraud", json.dumps(self.ok("get", f"{MINE}{self.t_low.id}/")))

    def test_detail_for_staff_has_summary_and_transcript(self):
        conv = self.conversation()
        self.say(conv, "domain trouble")
        Ticket.objects.filter(pk=self.t_low.pk).update(conversation=conv, summary="AI summary here")
        data = self.ok("get", f"{STAFF}{self.t_low.id}/")
        self.assertEqual(data["summary"], "AI summary here")
        self.assertEqual(data["transcript"][0]["content"], "domain trouble")


class NotificationTests(Base):
    def test_staff_are_emailed_when_a_ticket_is_raised(self):
        with override_settings(SUPPORT_EMAIL="support@example.com"), mock.patch.object(services, "send_store_email") as send:
            self.chat([reply(calls=[raise_call()]), reply("ok")], message="x")
        send.assert_called_once()
        subject, body, recipients = send.call_args.args[:3]
        self.assertEqual(recipients, ["support@example.com"])
        self.assertIn("TSS-000001", subject)
        self.assertIn("Followed the DNS steps", body)

    def test_no_staff_email_without_the_setting_and_a_failed_send_does_not_break_the_ticket(self):
        with override_settings(SUPPORT_EMAIL=""), mock.patch.object(services, "send_store_email") as send:
            self.chat([reply(calls=[raise_call()]), reply("ok")], message="x")
        send.assert_not_called()
        self.assertEqual(Ticket.objects.count(), 1)
        Ticket.objects.all().delete()
        with override_settings(SUPPORT_EMAIL="s@example.com"), mock.patch.object(services, "send_store_email", side_effect=RuntimeError):
            r, _ = self.chat([reply(calls=[raise_call()]), reply("ok")], message="y")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Ticket.objects.count(), 1)

    def test_merchant_is_emailed_on_staff_reply_and_status_change_but_not_notes(self):
        ticket = Ticket.objects.create(store=self.store_a, user=self.user_a, subject="Mine")
        self.as_staff(self.make_staff("agent@example.com", is_support_agent=True))
        with mock.patch.object(services, "send_store_email") as send:
            self.ok("post", f"{STAFF}{ticket.id}/messages/", {"message": "We fixed it, please retry"}, expect=201)
            self.assertEqual(send.call_args.args[2], ["a@example.com"])
            self.assertIn("We fixed it", send.call_args.args[1])
            self.assertIn(ticket.ticket_number, send.call_args.args[0])
            send.reset_mock()
            self.ok("patch", f"{STAFF}{ticket.id}/", {"status": "resolved"})
            self.assertEqual(send.call_args.args[2], ["a@example.com"])
            self.assertIn("resolved", send.call_args.args[0])
            send.reset_mock()
            self.ok("patch", f"{STAFF}{ticket.id}/", {"status": "resolved"})  # nothing changed
            self.ok("patch", f"{STAFF}{ticket.id}/", {"priority": "high"})    # not a status change
            send.assert_not_called()


class NumberingTests(Base):
    def test_numbers_are_sequential_and_unique(self):
        numbers = [Ticket.objects.create(store=self.store_a, user=self.user_a, subject=str(i)).ticket_number for i in range(3)]
        self.assertEqual(numbers, ["TSS-000001", "TSS-000002", "TSS-000003"])
        Ticket.objects.all().delete()
        self.assertEqual(Ticket.objects.create(store=self.store_a, user=self.user_a, subject="n").ticket_number, "TSS-000004")


class EscalationHelpersTests(Base):
    def test_clean_fields_cuts_and_defaults(self):
        values = escalation.clean_fields("  a  \n b " + "x" * 400, "s" * 9000, "weird", "weird")
        self.assertEqual(len(values["subject"]), escalation.MAX_SUBJECT)
        self.assertEqual(len(values["summary"]), escalation.MAX_SUMMARY)
        self.assertEqual((values["category"], values["priority"]), ("other", "normal"))
        self.assertEqual(escalation.clean_fields("", "", "", "")["subject"], "Help needed")

    def test_profile_fallback_reports_the_ticket_number(self):
        self.assertIn("TSS-000009", help_agent._fallback([{"ticket_number": "TSS-000009"}]))
        self.assertIn("raise a support ticket", help_agent._fallback([]))
