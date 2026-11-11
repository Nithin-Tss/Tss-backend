import json

from rest_framework.test import APITestCase

from apps.ai_assistant import services, tools
from apps.ai_assistant.models import AiPageDraft
from apps.themes.models import StorePage

from .helpers import TwoStores, make_collection, make_product

HERO = {"type": "hero", "settings": {"heading": "About us", "subheading": "Our story"}}
QUOTES = {"type": "custom", "settings": {"heading": "Reviews"}, "blocks": [
    {"type": "quote", "settings": {"text": "Lovely", "author": "Sam"}},
]}


class ToolTestBase(TwoStores, APITestCase):
    def setUp(self):
        self.make_stores()
        self.ca, self.cb = self.ctx(self.store_a), self.ctx(self.store_b)

    def run_tool(self, ctx, name, args):
        result, kind = tools.run_tool(ctx, name, json.dumps(args))
        return result, kind

    def make_page(self, ctx=None, sections=None):
        ctx = ctx or self.ca
        result, kind = self.run_tool(ctx, "create_page", {"title": "About us", "sections": sections or [HERO, QUOTES]})
        self.assertIsNone(kind, result)
        return result


class CreatePageTest(ToolTestBase):
    def test_creates_a_hidden_draft_with_its_own_template(self):
        result = self.make_page()
        page = StorePage.objects.get(pk=result["page_id"])
        self.assertEqual(page.status, StorePage.HIDDEN)
        self.assertIsNone(page.published_at)
        self.assertEqual(page.store_id, self.store_a.pk)
        self.assertTrue(page.template.startswith("page."))
        self.assertTrue(AiPageDraft.objects.get(page=page).created_by_ai)
        self.assertEqual(self.ca.changes[0]["action"], "created_page")
        shown = tools.get_page(self.ca, {"page_id": result["page_id"]})
        self.assertEqual([s["type"] for s in shown["sections"]], ["hero", "custom", "main-page"])

    def test_invalid_blocks_are_rejected_and_nothing_is_saved(self):
        for bad in (
            [{"type": "nope", "settings": {}}],
            [{"type": "hero", "settings": {"bogus": 1}}],
            [{"type": "custom", "blocks": [{"type": "nope"}]}],
            [{"type": "main-product"}],
        ):
            result, kind = self.run_tool(self.ca, "create_page", {"title": "X", "sections": bad})
            self.assertEqual(kind, "validation", bad)
            self.assertIn("error", result)
        self.assertFalse(StorePage.objects.exists())

    def test_html_is_sanitized_on_the_way_in(self):
        result = self.make_page(sections=[{"type": "custom", "settings": {}, "blocks": [
            {"type": "html", "settings": {"html": "<b>hi</b><script>steal()</script><img src=x onerror=steal()>"}},
        ]}])
        page = StorePage.objects.get(pk=result["page_id"])
        from apps.themes.services import get_store_theme

        stored = json.dumps(get_store_theme(self.store_a).custom_data["templates"][page.template])
        self.assertNotIn("<script", stored)
        self.assertNotIn("onerror", stored)

    def test_body_is_sanitized(self):
        result, _ = self.run_tool(self.ca, "create_page", {
            "title": "T", "sections": [HERO], "body": "<p>ok</p><script>x()</script>"})
        self.assertNotIn("<script", StorePage.objects.get(pk=result["page_id"]).body)


class SectionEditingTest(ToolTestBase):
    def test_add_update_delete_section(self):
        page_id = self.make_page(sections=[HERO])["page_id"]
        added, kind = self.run_tool(self.ca, "add_section", {"page_id": page_id, "section": QUOTES, "position": 0})
        self.assertIsNone(kind, added)
        self.assertEqual(added["position"], 0)
        sid = added["section_id"]

        updated, kind = self.run_tool(self.ca, "update_section", {
            "page_id": page_id, "section_id": sid, "changes": {"settings": {"heading": "Kind words"}}})
        self.assertIsNone(kind, updated)
        shown = tools.get_page(self.ca, {"page_id": page_id})
        self.assertEqual(shown["sections"][0]["settings"]["heading"], "Kind words")
        self.assertEqual(shown["sections"][0]["blocks"][0]["type"], "quote")  # blocks kept

        gone, kind = self.run_tool(self.ca, "delete_section", {"page_id": page_id, "section_id": sid})
        self.assertIsNone(kind, gone)
        self.assertNotIn("custom", [s["type"] for s in tools.get_page(self.ca, {"page_id": page_id})["sections"]])

    def test_default_position_is_above_the_page_content(self):
        page_id = self.make_page(sections=[HERO])["page_id"]
        self.run_tool(self.ca, "add_section", {"page_id": page_id, "section": QUOTES})
        types = [s["type"] for s in tools.get_page(self.ca, {"page_id": page_id})["sections"]]
        self.assertEqual(types, ["hero", "custom", "main-page"])

    def test_invalid_update_is_rejected_and_page_unchanged(self):
        page_id = self.make_page(sections=[HERO])["page_id"]
        sid = tools.get_page(self.ca, {"page_id": page_id})["sections"][0]["id"]
        result, kind = self.run_tool(self.ca, "update_section", {
            "page_id": page_id, "section_id": sid, "changes": {"settings": {"height": "huge"}}})
        self.assertEqual(kind, "validation")
        self.assertEqual(tools.get_page(self.ca, {"page_id": page_id})["sections"][0]["settings"]["heading"], "About us")

    def test_page_content_section_is_protected(self):
        page_id = self.make_page(sections=[HERO])["page_id"]
        main = tools.get_page(self.ca, {"page_id": page_id})["sections"][-1]
        for name, extra in (("delete_section", {}), ("update_section", {"changes": {"hidden": True}})):
            _, kind = self.run_tool(self.ca, name, {"page_id": page_id, "section_id": main["id"], **extra})
            self.assertEqual(kind, "refused", name)

    def test_published_pages_cannot_be_edited(self):
        page_id = self.make_page()["page_id"]
        StorePage.objects.filter(pk=page_id).update(status=StorePage.VISIBLE)
        result, kind = self.run_tool(self.ca, "add_section", {"page_id": page_id, "section": HERO})
        self.assertEqual(kind, "refused")
        self.assertIn("published", result["error"])

    def test_existing_hidden_page_gets_its_own_template(self):
        page = StorePage.objects.create(store=self.store_a, title="Old", handle="old", status=StorePage.HIDDEN)
        result, kind = self.run_tool(self.ca, "add_section", {"page_id": str(page.pk), "section": HERO})
        self.assertIsNone(kind, result)
        page.refresh_from_db()
        self.assertTrue(page.template.startswith("page."))
        self.assertFalse(AiPageDraft.objects.get(page=page).created_by_ai)


class IsolationTest(ToolTestBase):
    """Merchant A can never read or touch merchant B's data, whatever ids the model sends."""

    def test_other_merchants_page_is_invisible_and_untouchable(self):
        page_id = self.make_page(self.cb)["page_id"]
        sid = tools.get_page(self.cb, {"page_id": page_id})["sections"][0]["id"]
        for name, args in (
            ("get_page", {"page_id": page_id}),
            ("add_section", {"page_id": page_id, "section": HERO}),
            ("update_section", {"page_id": page_id, "section_id": sid, "changes": {"settings": {"heading": "Hacked"}}}),
            ("delete_section", {"page_id": page_id, "section_id": sid}),
        ):
            result, kind = self.run_tool(self.ca, name, args)
            self.assertEqual(kind, "refused", name)
            self.assertIn("No such page", result["error"])
        shown = tools.get_page(self.cb, {"page_id": page_id})
        self.assertEqual(shown["sections"][0]["settings"]["heading"], "About us")
        self.assertEqual(self.ca.changes, [])

    def test_list_pages_only_shows_own_pages(self):
        self.make_page(self.ca)
        self.make_page(self.cb)
        listed = tools.list_pages(self.ca, {})["pages"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(StorePage.objects.get(pk=listed[0]["id"]).store_id, self.store_a.pk)

    def test_products_and_collections_are_own_only(self):
        mine, theirs = make_product(self.store_a, "Mine"), make_product(self.store_b, "Theirs")
        self.assertEqual([p["title"] for p in tools.list_products(self.ca, {})["products"]], ["Mine"])
        make_collection(self.store_a, "A coll"), make_collection(self.store_b, "B coll")
        self.assertEqual([c["name"] for c in tools.list_collections(self.ca, {})["collections"]], ["A coll"])
        # a section may not point at another merchant's product
        section = {"type": "custom", "settings": {}, "blocks": [{"type": "products", "settings": {"products": [str(theirs.pk)]}}]}
        result, kind = self.run_tool(self.ca, "create_page", {"title": "P", "sections": [section]})
        self.assertEqual(kind, "validation")
        self.assertIn("Unknown product", result["error"])
        section["blocks"][0]["settings"]["products"] = [str(mine.pk)]
        _, kind = self.run_tool(self.ca, "create_page", {"title": "P", "sections": [section]})
        self.assertIsNone(kind)

    def test_model_cannot_pass_a_store(self):
        page_id = self.make_page(self.cb)["page_id"]
        result, kind = self.run_tool(self.ca, "get_page", {"page_id": page_id, "store_id": str(self.store_b.pk)})
        self.assertEqual(kind, "refused")

    def test_templates_of_other_pages_are_not_reachable(self):
        # the base templates (home, product...) are not something any tool can name
        page = StorePage.objects.create(store=self.store_a, title="P", handle="p", status=StorePage.HIDDEN, template="index")
        self.run_tool(self.ca, "add_section", {"page_id": str(page.pk), "section": HERO})
        from apps.themes.services import get_store_theme

        index = get_store_theme(self.store_a).custom_data.get("templates", {}).get("index", {})
        self.assertNotIn("hero-1", index.get("sections", {}))
        self.assertNotEqual(StorePage.objects.get(pk=page.pk).template, "index")


class RunToolTest(ToolTestBase):
    def test_malformed_unknown_and_wrong_shape(self):
        self.assertEqual(tools.run_tool(self.ca, "list_pages", "{not json")[1], "malformed")
        self.assertEqual(tools.run_tool(self.ca, "list_pages", "[1]")[1], "malformed")
        self.assertEqual(tools.run_tool(self.ca, "drop_database", "{}")[1], "unknown")
        self.assertEqual(tools.run_tool(self.ca, "get_page", "{}")[1], "validation")
        self.assertEqual(tools.run_tool(self.ca, "get_page", '{"page_id": "not-a-uuid"}')[1], "refused")

    def test_there_is_no_publish_tool(self):
        self.assertFalse({"publish_page", "publish_theme", "duplicate_theme"} & set(tools.TOOLS))


class DraftLifecycleTest(ToolTestBase):
    def test_publish_makes_the_page_visible(self):
        page_id = self.make_page()["page_id"]
        page = services.publish_draft(self.store_a, page_id)
        self.assertEqual(page.status, StorePage.VISIBLE)
        self.assertIsNotNone(page.published_at)
        self.assertFalse(AiPageDraft.objects.filter(page_id=page_id).exists())

    def test_discard_removes_a_page_the_assistant_created(self):
        page_id = self.make_page()["page_id"]
        services.discard_draft(self.store_a, page_id)
        self.assertFalse(StorePage.objects.filter(pk=page_id).exists())

    def test_discard_restores_a_page_it_only_edited(self):
        page = StorePage.objects.create(store=self.store_a, title="Old", handle="old", status=StorePage.HIDDEN)
        self.run_tool(self.ca, "add_section", {"page_id": str(page.pk), "section": HERO})
        services.discard_draft(self.store_a, page.pk)
        page.refresh_from_db()
        self.assertEqual(page.template, "")

    def test_other_merchant_cannot_publish_or_discard(self):
        from rest_framework.exceptions import NotFound

        page_id = self.make_page(self.ca)["page_id"]
        for fn in (services.publish_draft, services.discard_draft):
            with self.assertRaises(NotFound):
                fn(self.store_b, page_id)
        self.assertEqual(StorePage.objects.get(pk=page_id).status, StorePage.HIDDEN)

    def test_pages_the_assistant_did_not_make_cannot_be_published_through_it(self):
        from rest_framework.exceptions import NotFound

        page = StorePage.objects.create(store=self.store_a, title="Mine", handle="mine", status=StorePage.HIDDEN)
        with self.assertRaises(NotFound):
            services.publish_draft(self.store_a, page.pk)
