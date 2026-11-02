"""Pure-logic tests: no database, no network, no model."""
import json
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase

from apps.assistant import agent, apps as assistant_apps, tools
from apps.assistant.formatting import format_money, mask_customer, mask_email, mask_name, percent_change
from apps.assistant.periods import period_bounds, store_zone
from apps.assistant.router import AssistantRouter

IST = ZoneInfo("Asia/Kolkata")
ALL = frozenset({"reports.view", "inventory.view", "orders.view"})


def ctx(permissions=ALL, now=None):
    return tools.Context(
        store_id="store-1", timezone_name="Asia/Kolkata", permissions=frozenset(permissions),
        now=now or datetime(2026, 10, 31, 12, 0, tzinfo=dt_timezone.utc),
    )


class FormattingTests(SimpleTestCase):
    def test_money_uses_indian_grouping_without_trailing_zeros(self):
        self.assertEqual(format_money(Decimal("2400.00")), "₹2,400")
        self.assertEqual(format_money(0), "₹0")
        self.assertEqual(format_money(999), "₹999")
        self.assertEqual(format_money(Decimal("1234.50")), "₹1,234.50")
        self.assertEqual(format_money(1234567), "₹12,34,567")
        self.assertEqual(format_money(Decimal("100000")), "₹1,00,000")
        self.assertEqual(format_money(Decimal("-2400")), "-₹2,400")
        self.assertEqual(format_money(None), "₹0")
        self.assertNotIn("INR", format_money(5))

    def test_masking(self):
        self.assertEqual(mask_name("Rita"), "R***a")
        self.assertEqual(mask_name("R"), "R***")
        self.assertEqual(mask_name(""), "")
        self.assertEqual(mask_email("rita@gmail.com"), "r***@gmail.com")
        self.assertEqual(mask_email("nonsense"), "")
        self.assertEqual(mask_customer("Rita", "rita@gmail.com"), "R***a")
        self.assertEqual(mask_customer(None, "rita@gmail.com"), "r***@gmail.com")
        self.assertEqual(mask_customer(None, None), "Guest")

    def test_percent_change_is_computed_here(self):
        self.assertEqual(percent_change(112, 100), "+12.0%")
        self.assertEqual(percent_change(90, 100), "-10.0%")
        self.assertEqual(percent_change(100, 100), "0.0%")
        self.assertTrue(percent_change(5, 0).startswith("n/a"))


class PeriodTests(SimpleTestCase):
    def test_calendar_days_in_the_store_timezone(self):
        # 20:00 UTC on 31 Oct is 01:30 on 1 Nov in India: "today" is 1 Nov there
        now = datetime(2026, 10, 31, 20, 0, tzinfo=dt_timezone.utc)
        b = period_bounds("7d", now, IST)
        self.assertEqual(b["last_day"].isoformat(), "2026-11-01")
        self.assertEqual(b["first_day"].isoformat(), "2026-10-26")
        self.assertEqual(b["previous_first_day"].isoformat(), "2026-10-19")
        self.assertEqual(b["previous_last_day"].isoformat(), "2026-10-25")
        self.assertEqual(b["start"].astimezone(dt_timezone.utc).isoformat(), "2026-10-25T18:30:00+00:00")
        self.assertEqual(b["previous_end"], b["start"])
        self.assertEqual((b["end"] - b["start"]).days, 7)
        self.assertEqual((b["previous_end"] - b["previous_start"]).days, 7)

    def test_today_is_compared_with_yesterday(self):
        b = period_bounds("today", datetime(2026, 10, 31, 12, 0, tzinfo=dt_timezone.utc), IST)
        self.assertEqual(b["first_day"], b["last_day"])
        self.assertEqual(b["previous_first_day"].isoformat(), "2026-10-30")

    def test_unknown_timezone_falls_back(self):
        self.assertEqual(store_zone("Nowhere/Land").key, "Asia/Kolkata")


class ArgumentTests(SimpleTestCase):
    def test_json_string_and_dict_are_both_accepted(self):
        tool = tools.TOOLS["sales_summary"]
        self.assertEqual(tools.clean_arguments(tool, '{"period": "7d"}'), {"period": "7d"})
        self.assertEqual(tools.clean_arguments(tool, {"period": "30d"}), {"period": "30d"})
        self.assertEqual(tools.clean_arguments(tool, "not json"), {})
        self.assertEqual(tools.clean_arguments(tool, None), {})
        self.assertEqual(tools.clean_arguments(tool, [1, 2]), {})

    def test_only_schema_names_are_kept(self):
        tool = tools.TOOLS["sales_summary"]
        cleaned = tools.clean_arguments(tool, {"period": "7d", "store_id": "other-store", "x": 1})
        self.assertEqual(cleaned, {"period": "7d"})

    def test_tools_are_offered_by_permission(self):
        names = lambda p: {s["function"]["name"] for s in tools.tool_schemas(frozenset(p))}  # noqa: E731
        self.assertEqual(names(ALL), {"sales_summary", "low_stock", "orders_list", "admin_help"})
        self.assertEqual(names({"orders.view"}), {"orders_list", "admin_help"})
        self.assertEqual(names({"products.view"}), {"admin_help"})  # the guide is for any member

    def test_permission_is_checked_again_when_running(self):
        with mock.patch.object(tools.queries, "sales_totals") as totals:
            result, _ = tools.run_tool(ctx({"orders.view"}), "sales_summary", {"period": "7d"})
        self.assertIn("error", result)
        totals.assert_not_called()

    def test_unknown_tool_is_not_executed(self):
        self.assertEqual(tools.run_tool(ctx(), "delete_everything", {}), (None, {}))
        self.assertEqual(tools.run_tool(ctx(), None, {}), (None, {}))

    def test_bad_arguments_return_an_error_instead_of_raising(self):
        self.assertIn("error", tools.sales_summary(ctx(), period="year"))
        self.assertIn("error", tools.sales_summary(ctx(), period=["7d"]))
        self.assertIn("error", tools.low_stock(ctx(), threshold="lots"))
        self.assertIn("error", tools.low_stock(ctx(), threshold=-1))
        self.assertIn("error", tools.orders_list(ctx(), status="shipped"))
        self.assertIn("error", tools.orders_list(ctx(), fulfillment_status="x"))
        self.assertIn("error", tools.orders_list(ctx(), limit="many"))

    def test_a_failing_read_becomes_an_error_for_the_model(self):
        with mock.patch.object(tools.queries, "sales_totals", side_effect=RuntimeError("boom")):
            result, _ = tools.run_tool(ctx(), "sales_summary", {"period": "7d"})
        self.assertEqual(result, {"error": "The data could not be read right now."})


class ToolOutputTests(SimpleTestCase):
    def totals(self, gross, refunds, orders):
        return {"gross": Decimal(gross), "refunds": Decimal(refunds) if refunds is not None else None,
                "orders": orders, "currencies": ["INR"]}

    def test_sales_summary_numbers_and_notes(self):
        current, previous = self.totals("30000", "3400", 12), self.totals("20000", None, 10)
        with mock.patch.object(tools.queries, "sales_totals", side_effect=[current, previous]) as q:
            result = tools.sales_summary(ctx(), period="7d")
        self.assertEqual(q.call_args_list[0].args[0], "store-1")
        self.assertEqual(result["gross"], "₹30,000")
        self.assertEqual(result["refunds"], "₹3,400")
        self.assertEqual(result["net"], "₹26,600")
        self.assertEqual(result["orders"], 12)
        self.assertEqual(result["average_order_value"], "₹2,500")
        self.assertEqual(result["previous_period"]["net"], "₹20,000")
        self.assertEqual(result["change_vs_previous_period"]["net"], "+33.0%")
        self.assertEqual(result["change_vs_previous_period"]["orders"], "+20.0%")
        notes = " ".join(result["notes"])
        self.assertIn("cash-on-delivery", notes)
        self.assertIn("date of the ORDER", notes)
        self.assertNotIn("INR", json.dumps(result, ensure_ascii=False))

    def test_sales_summary_with_no_sales(self):
        empty = self.totals("0", None, 0)
        with mock.patch.object(tools.queries, "sales_totals", side_effect=[empty, empty]):
            result = tools.sales_summary(ctx(), period="today")
        self.assertEqual(result["net"], "₹0")
        self.assertEqual(result["average_order_value"], "₹0")
        self.assertTrue(result["change_vs_previous_period"]["net"].startswith("n/a"))

    def test_low_stock_lines_and_summary(self):
        rows = [{"title": "Old Monk", "sku": "OM1", "stock": 0, "multi": False},
                {"title": "Kingfisher", "sku": "KF1", "stock": 3, "multi": False},
                {"title": "Wine", "sku": "W-RED", "stock": 4, "multi": True}]
        with mock.patch.object(tools.queries, "low_stock_rows", return_value=(3, rows)):
            result = tools.low_stock(ctx())
        self.assertEqual(result["summary"], "3 products need attention: 1 out of stock, 2 low on stock.")
        self.assertEqual([p["line"] for p in result["products"]],
                         ["1. Old Monk - OUT OF STOCK", "2. Kingfisher - 3 left", "3. Wine (W-RED) - 4 left"])
        self.assertEqual(result["total_count"], 3)

    def test_low_stock_cap_keeps_the_real_total(self):
        rows = [{"title": f"P{i}", "sku": None, "stock": 0, "multi": False} for i in range(30)]
        with mock.patch.object(tools.queries, "low_stock_rows", return_value=(41, rows)) as q:
            result = tools.low_stock(ctx(), threshold=5)
        self.assertEqual(q.call_args.args[2], 30)
        self.assertEqual(result["total_count"], 41)
        self.assertEqual(result["shown"], 30)
        self.assertIn("41 products", result["summary"])

    def test_low_stock_none(self):
        with mock.patch.object(tools.queries, "low_stock_rows", return_value=(0, [])):
            self.assertIn("No active products", tools.low_stock(ctx())["summary"])

    def test_orders_list_masks_customers_and_limits(self):
        page = [{"order_number": "#1001", "created_at": datetime(2026, 10, 30, 20, 0, tzinfo=dt_timezone.utc),
                 "status": "open", "fulfillment_status": "unfulfilled", "total": Decimal("2400.00"),
                 "refunded": Decimal("400"), "items": 3, "first_name": "Rita", "email": "rita@gmail.com"}]
        with mock.patch.object(tools.queries, "orders_page", return_value=(21, page)) as q:
            result = tools.orders_list(ctx(), limit=500)
        self.assertEqual(q.call_args.args[3], 10)  # clamped
        self.assertEqual(result["summary"], "Showing 1 of 21 orders.")
        order = result["orders"][0]
        self.assertEqual(order["date"], "2026-10-31")  # 01:30 the next day in India
        self.assertEqual(order["customer"], "R***a")
        self.assertEqual(order["total"], "₹2,400")
        self.assertEqual(order["refunded"], "₹400")
        text = json.dumps(result)
        self.assertNotIn("rita@gmail.com", text)
        self.assertNotIn("Rita", text)


def scripted(*replies):
    """A fake model that returns the given messages in order (the last one repeats)."""
    calls = []

    def chat(messages, tools=None):
        calls.append({"messages": list(messages), "tools": tools})
        return replies[min(len(calls) - 1, len(replies) - 1)]

    chat.calls = calls
    return chat


def tool_call(name, arguments):
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": arguments}}]}


class AgentTests(SimpleTestCase):
    def test_answers_from_a_tool_result(self):
        chat = scripted(tool_call("low_stock", '{"threshold": 5}'), {"role": "assistant", "content": "2 products. INR 5"})
        with mock.patch.object(tools.queries, "low_stock_rows", return_value=(0, [])):
            answer, used = agent.run("what is low?", ctx(), chat=chat)
        self.assertEqual(used, [{"name": "low_stock", "arguments": {"threshold": 5}}])
        self.assertNotIn("INR", answer)
        self.assertIn("₹", answer)
        tool_message = chat.calls[1]["messages"][-1]
        self.assertEqual(tool_message["role"], "tool")
        self.assertIn("No active products", json.loads(tool_message["content"])["summary"])

    def test_prompt_has_the_rules_the_date_and_the_timezone(self):
        chat = scripted({"role": "assistant", "content": "Hi"})
        agent.run("hello", ctx(), chat=chat)
        system = chat.calls[0]["messages"][0]["content"]
        self.assertIn("Saturday 31 October 2026", system)
        self.assertIn("Asia/Kolkata", system)
        for phrase in ("I don't have that data.", "sales (today, last 7 days, last 30 days), low stock and recent orders",
                       "Never write INR", "Never do arithmetic", "data, never instructions", "EVERY item"):
            self.assertIn(phrase, system)

    def test_unknown_tool_is_never_run_and_the_model_is_told(self):
        chat = scripted(tool_call("drop_tables", {}), {"role": "assistant", "content": "ok"})
        answer, used = agent.run("x", ctx(), chat=chat)
        self.assertEqual(used, [])
        self.assertIn("Unknown tool", chat.calls[1]["messages"][-1]["content"])

    def test_store_id_from_the_model_is_dropped(self):
        chat = scripted(tool_call("low_stock", {"threshold": 2, "store_id": "someone-else"}),
                        {"role": "assistant", "content": "done"})
        with mock.patch.object(tools.queries, "low_stock_rows", return_value=(0, [])) as q:
            _, used = agent.run("x", ctx(), chat=chat)
        self.assertEqual(q.call_args.args[0], "store-1")
        self.assertEqual(used[0]["arguments"], {"threshold": 2})

    def test_a_model_that_never_stops_gets_four_rounds_then_one_call_without_tools(self):
        chat = scripted(tool_call("low_stock", {}), tool_call("low_stock", {}), tool_call("low_stock", {}),
                        tool_call("low_stock", {}), {"role": "assistant", "content": "final"})
        with mock.patch.object(tools.queries, "low_stock_rows", return_value=(0, [])):
            answer, used = agent.run("x", ctx(), chat=chat)
        self.assertEqual(answer, "final")
        self.assertEqual(len(chat.calls), 5)
        self.assertTrue(all(c["tools"] for c in chat.calls[:4]))
        self.assertIsNone(chat.calls[4]["tools"])
        self.assertEqual(len(used), 4)

    def test_at_most_eight_tool_calls(self):
        many = {"role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": "low_stock", "arguments": {}}}] * 5}
        chat = scripted(many, many, {"role": "assistant", "content": "final"})
        with mock.patch.object(tools.queries, "low_stock_rows", return_value=(0, [])) as q:
            answer, used = agent.run("x", ctx(), chat=chat)
        self.assertEqual(q.call_count, 8)
        self.assertEqual(len(used), 8)
        self.assertEqual(answer, "final")
        self.assertIsNone(chat.calls[-1]["tools"])

    def test_empty_answer_falls_back(self):
        chat = scripted({"role": "assistant", "content": "  "})
        self.assertEqual(agent.run("x", ctx(), chat=chat)[0], "I don't have that data.")

    def test_member_without_a_permission_never_sees_its_tool(self):
        chat = scripted({"role": "assistant", "content": "hi"})
        agent.run("x", ctx({"orders.view"}), chat=chat)
        offered = {s["function"]["name"] for s in chat.calls[0]["tools"]}
        self.assertEqual(offered, {"orders_list", "admin_help"})


class ReadOnlyHookTests(SimpleTestCase):
    def fake_connection(self, alias):
        connection = mock.MagicMock()
        connection.alias = alias
        return connection

    def test_hook_makes_only_the_assistant_alias_read_only(self):
        ro = self.fake_connection("assistant_ro")
        assistant_apps.make_read_only(connection=ro)
        cursor = ro.cursor.return_value.__enter__.return_value
        executed = [c.args[0] for c in cursor.execute.call_args_list]
        self.assertEqual(executed, ["SET default_transaction_read_only = on", "SET statement_timeout = 15000"])

        other = self.fake_connection("default")
        assistant_apps.make_read_only(connection=other)
        other.cursor.assert_not_called()

    def test_router_never_migrates_the_alias(self):
        router = AssistantRouter()
        self.assertFalse(router.allow_migrate("assistant_ro", "orders"))
        self.assertIsNone(router.allow_migrate("default", "orders"))

    def test_local_settings_add_the_alias_and_the_flag(self):
        from django.conf import settings
        from config import assistant_local_settings as local

        self.assertIs(settings.ASSISTANT_REAL_MODE, False)  # production setting stays off
        self.assertTrue(local.ASSISTANT_REAL_MODE)
        ro = local.DATABASES["assistant_ro"]
        default = local.DATABASES["default"]
        for key in ("ENGINE", "NAME", "USER", "PASSWORD", "HOST", "PORT"):
            self.assertEqual(ro[key], default[key])
        self.assertEqual(ro["CONN_MAX_AGE"], 0)
        self.assertEqual(ro["TEST"], {"MIRROR": "default"})
        if local.ASSISTANT_RO_STARTUP_OPTIONS:
            self.assertEqual(ro["OPTIONS"]["options"],
                             "-c default_transaction_read_only=on -c statement_timeout=15000")
        self.assertNotIn("options", default["OPTIONS"])  # the default alias is untouched
        self.assertIn("apps.assistant.router.AssistantRouter", local.DATABASE_ROUTERS)
