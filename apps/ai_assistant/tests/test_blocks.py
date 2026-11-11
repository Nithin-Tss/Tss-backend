from django.test import SimpleTestCase

from apps.ai_assistant import blocks


def tpl(*sections):
    order = [f"s{i}" for i in range(len(sections))]
    found = {f"s{i}": s for i, s in enumerate(sections)}
    # The theme requires the page-content section on every page template
    return {"order": [*order, "main"], "sections": {**found, "main": {"type": "main-page", "settings": {}}}}


class ValidationTest(SimpleTestCase):
    def test_valid_hero_and_custom_pass(self):
        clean = blocks.validate_template("page.x", tpl(
            {"type": "hero", "settings": {"heading": "About us", "button_label": "Shop", "button_link": "/collections/all"}},
            {"type": "custom", "settings": {"heading": "Reviews"}, "blocks": [
                {"type": "quote", "settings": {"text": "Great!", "author": "Sam"}},
            ]},
        ))
        self.assertEqual(clean["sections"]["s0"]["settings"]["heading"], "About us")
        self.assertEqual(clean["sections"]["s1"]["blocks"][0]["type"], "quote")

    def test_unknown_section_type_is_rejected(self):
        with self.assertRaisesMessage(blocks.BlockError, "Unknown section type 'carousel-of-doom'"):
            blocks.validate_template("page.x", tpl({"type": "carousel-of-doom", "settings": {}}))

    def test_theme_sections_outside_the_allowlist_are_rejected(self):
        for kind in ("main-product", "main-collection", "product-grid", "main-checkout"):
            with self.assertRaises(blocks.BlockError, msg=kind):
                blocks.validate_template("page.x", tpl({"type": kind, "settings": {}}))

    def test_unknown_block_type_is_rejected(self):
        with self.assertRaisesMessage(blocks.BlockError, "unknown block type 'laser'"):
            blocks.validate_template("page.x", tpl({"type": "custom", "settings": {}, "blocks": [{"type": "laser"}]}))

    def test_unknown_setting_is_rejected(self):
        with self.assertRaisesMessage(blocks.BlockError, "unknown settings: sparkle"):
            blocks.validate_template("page.x", tpl({"type": "hero", "settings": {"sparkle": 1}}))

    def test_invalid_value_is_rejected_with_a_usable_message(self):
        with self.assertRaisesMessage(blocks.BlockError, "must be one of"):
            blocks.validate_template("page.x", tpl({"type": "hero", "settings": {"height": "gigantic"}}))
        with self.assertRaises(blocks.BlockError):
            blocks.validate_template("page.x", tpl({"type": "hero", "settings": {"button_link": "javascript:alert(1)"}}))

    def test_custom_html_is_sanitized(self):
        clean = blocks.validate_template("page.x", tpl({"type": "custom", "settings": {}, "blocks": [
            {"type": "html", "settings": {"html": "<p onclick='x()'>hi</p><script>alert(1)</script>"}},
            {"type": "text", "settings": {"body": "<p>ok</p><script>alert(2)</script>"}},
        ]}))
        blob = str(clean)
        self.assertNotIn("<script", blob)
        self.assertNotIn("onclick", blob)
        self.assertIn("hi", blob)

    def test_too_many_sections(self):
        many = tpl(*[{"type": "rich-text", "settings": {}} for _ in range(blocks.MAX_AI_SECTIONS + 1)])
        with self.assertRaisesMessage(blocks.BlockError, "at most"):
            blocks.validate_template("page.x", many)


class SchemaGenerationTest(SimpleTestCase):
    def test_tool_schemas_come_from_the_theme_schema(self):
        from apps.themes import schemas

        for kind in blocks.section_types():
            self.assertIn(kind, schemas.SECTIONS)
        tools = {t["function"]["name"]: t["function"] for t in blocks.tool_definitions()}
        self.assertEqual(
            tools["create_page"]["parameters"]["properties"]["sections"]["items"]["properties"]["type"]["enum"],
            blocks.section_types(),
        )
        for tool in tools.values():  # the store is never something the model chooses
            self.assertNotIn("store", tool["parameters"]["properties"])
            self.assertNotIn("store_id", tool["parameters"]["properties"])

    def test_describe_lists_real_settings_and_options(self):
        hero = blocks.describe_section_type("hero")
        self.assertIn("heading", hero["settings"])
        self.assertIn("quote", blocks.describe_section_type("custom")["blocks"])
        with self.assertRaises(ValueError):
            blocks.describe_section_type("main-product")
