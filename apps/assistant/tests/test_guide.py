"""The admin guide, its search, the action whitelist and admin_help. No database."""
import re
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from apps.assistant import actions, agent, guide, tools
from apps.assistant.actions import ACTIONS, build_actions

STARTER = Path(__file__).resolve().parents[2] / "themes" / "starter" / "sections"


def ctx(permissions=frozenset()):
    return tools.Context(store_id="store-1", timezone_name="Asia/Kolkata", permissions=frozenset(permissions))


class GuideFileTests(SimpleTestCase):
    def test_every_entry_is_complete_and_opens_a_whitelisted_page(self):
        entries = guide.load()
        self.assertGreaterEqual(len(entries), 12)
        ids = [e.id for e in entries]
        self.assertEqual(len(ids), len(set(ids)))
        for e in entries:
            self.assertTrue(e.task and e.keywords and e.steps, e.id)
            self.assertIn(e.open, ACTIONS, f"{e.id} opens an unknown page key")

    def test_a_malformed_entry_is_rejected(self):
        with self.assertRaises(ValueError):
            guide.parse("## x\ntask: t\nkeywords: k\nopen: customize\n")
        with self.assertRaises(ValueError):
            guide.parse("## x\nkeywords: k\nopen: customize\nsteps:\n1. a\n")

    def test_quoted_theme_labels_exist_in_the_starter_theme(self):
        header = (STARTER / "header.liquid").read_text(encoding="utf-8")
        footer = (STARTER / "footer.liquid").read_text(encoding="utf-8")
        text = Path(guide.GUIDE_FILE).read_text(encoding="utf-8")
        for label in ("Logo text", "Logo image", "Logo image height", "Logo position", "Announcement text",
                      "Announcement link", "Announcement colour", "Announcement text colour", "Show the navigation menu",
                      "Collections in the automatic menu", "Label", "Link"):
            self.assertIn(f'"label": "{label}"', header, label)
            self.assertIn(label, text, label)
        self.assertIn('"name": "Menu link"', header)  # a block name, not a field label
        self.assertIn("Menu link", text)
        for label in ("Copyright line", "Show the utility bar", "Utility bar text", "Phone number", "Email address"):
            self.assertIn(f'"label": "{label}"', footer, label)
            self.assertIn(label, text, label)
        for block in ('"name": "Logo"', '"name": "Text"', '"name": "Links"'):
            self.assertIn(block, footer, block)
        self.assertIn("sections/header.liquid", text)
        self.assertIn("sections/footer.liquid", text)

    def test_no_file_names_other_than_the_ones_the_editor_shows(self):
        text = Path(guide.GUIDE_FILE).read_text(encoding="utf-8")
        named = set(re.findall(r"[\w./-]+\.(?:liquid|css|js|json)\b", text))
        self.assertLessEqual(named, {"sections/header.liquid", "sections/footer.liquid", "header.liquid", "footer.liquid"})


class SearchTests(SimpleTestCase):
    def best(self, topic):
        entry, _ = guide.search(topic)
        return entry.id if entry else None

    def test_the_questions_owners_ask(self):
        cases = {
            "how do I change the logo text in the header": "logo-text",
            "change header logo text": "logo-text",
            "where do I upload my logo picture": "logo-image",
            "change the announcement bar": "announcement-bar",
            "I want to hide the top banner": "announcement-bar",
            "edit the footer": "footer",
            "change the copyright line": "footer",
            "edit theme code": "edit-theme-code",
            "where is the code editor": "edit-theme-code",
            "edit header.liquid": "edit-header-footer-code",
            "add a product": "add-product",
            "create a new product": "add-product",
            "change the price of a product": "product-price-visibility",
            "create a discount code": "create-discount",
            "how to make a coupon": "create-discount",
            "add a link to the header menu": "header-menu-links",
            "create a footer menu": "menus",
            "add an about page": "add-page",
            "publish my new theme": "publish-theme",
        }
        for question, expected in cases.items():
            self.assertEqual(self.best(question), expected, question)

    def test_unknown_topics_find_nothing(self):
        for topic in ("how do I rotate my tyres", "quantum chromodynamics", "", "   ", "the a an"):
            self.assertEqual(self.best(topic), None, topic)

    def test_close_alternatives_are_limited(self):
        _, related = guide.search("edit code")
        self.assertLessEqual(len(related), 2)


class AdminHelpToolTests(SimpleTestCase):
    def test_returns_numbered_steps_and_the_open_path_and_touches_no_database(self):
        with mock.patch.object(tools.queries, "alias_configured", side_effect=AssertionError("database used")), \
                mock.patch.object(tools.queries, "ensure_read_only", side_effect=AssertionError("database used")):
            c = ctx()
            result, args = tools.run_tool(c, "admin_help", {"topic": "change header logo text"})
        self.assertTrue(result["found"])
        self.assertEqual(args, {"topic": "change header logo text"})
        self.assertTrue(result["steps"][0].startswith("1. "))
        self.assertTrue(any("Logo text" in s for s in result["steps"]))
        self.assertEqual(result["open_path"], "/admin/online-store/customize")
        self.assertEqual(c.open_keys, ["customize"])

    def test_not_in_the_guide_says_so_and_opens_nothing(self):
        c = ctx()
        result, _ = tools.run_tool(c, "admin_help", {"topic": "fix my car"})
        self.assertFalse(result["found"])
        self.assertIn("do not guess", result["message"])
        self.assertEqual(c.open_keys, [])
        self.assertNotIn("steps", result)

    def test_bad_topics_return_an_error(self):
        for topic in ("", "  ", None, 5, ["x"]):
            result, _ = tools.run_tool(ctx(), "admin_help", {"topic": topic})
            self.assertIn("error", result, topic)

    def test_a_path_in_the_arguments_is_ignored(self):
        c = ctx()
        _, args = tools.run_tool(c, "admin_help", {"topic": "add a product", "open_path": "https://evil.example"})
        self.assertEqual(args, {"topic": "add a product"})
        self.assertEqual(c.open_keys, ["add_product"])

    def test_works_for_a_member_with_no_permissions_at_all(self):
        result, _ = tools.run_tool(ctx(), "admin_help", {"topic": "create a discount"})
        self.assertTrue(result["found"])

    def test_the_agent_prompt_asks_for_the_guide(self):
        self.assertIn("admin_help", agent.SYSTEM_PROMPT)
        self.assertIn("I don't have a guide for that yet.", agent.SYSTEM_PROMPT)


class ActionWhitelistTests(SimpleTestCase):
    def test_paths_are_plain_admin_routes(self):
        for key, action in actions.ACTIONS.items():
            self.assertRegex(action.path, r"^/admin/online-store(/[a-z-]+)*$", key)
        self.assertIn("/admin/online-store/customize", [a.path for a in ACTIONS.values()])

    def test_only_known_keys_become_buttons(self):
        everything = frozenset(a.permission for a in ACTIONS.values())
        built = build_actions(["customize", "/admin/evil", "javascript:alert(1)", None, 7, "code"], everything)
        self.assertEqual([b["path"] for b in built],
                         ["/admin/online-store/customize", "/admin/online-store/code"])

    def test_a_member_without_the_permission_gets_no_button(self):
        self.assertEqual(build_actions(["customize"], frozenset({"themes.view"})), [])
        self.assertEqual(build_actions(["add_product"], frozenset({"products.view"})), [])
        self.assertEqual(len(build_actions(["add_product"], frozenset({"products.manage"}))), 1)

    def test_no_repeats_and_at_most_three(self):
        everything = frozenset(a.permission for a in ACTIONS.values())
        keys = ["customize", "customize", "code", "menus", "pages", "products"]
        built = build_actions(keys, everything)
        self.assertEqual(len(built), 3)
        self.assertEqual(len({b["path"] for b in built}), 3)
