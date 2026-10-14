"""
Two-way sync of the synced sections: sections/<name>.liquid <-> the customizer's panel.
The header and the footer run the same checks through SyncedSectionMixin.

    python manage.py test apps.themes --settings=config.test_settings
"""
import hashlib
import json
import time

from rest_framework.test import APITestCase

from apps.core.tests.test_flow import StoreClientMixin

from . import section_sync as sync


class SyncedSectionMixin(StoreClientMixin):
    """Helpers for one synced section (NAME); each test class measures its own timings."""

    NAME = None
    EMAIL = None
    timings = None

    @property
    def PATH(self):
        return sync.SYNCED_SECTIONS[self.NAME]["path"]

    def setUp(self):
        self.token, self.store = self.sign_up(self.EMAIL, f"{self.NAME.title()} Store")

    # ---------------------------------------------------------------- helpers

    def code(self):
        files = self.ok("get", "/api/v1/themes/files/")["files"]
        return next(f["content"] for f in files if f["path"] == self.PATH)

    def section(self):
        return self.ok("get", "/api/v1/themes/sections/")[self.NAME]

    def save_values(self, values, version=None):
        started = time.perf_counter()
        body = {"sections": {self.NAME: values}, "client": "test"}
        if version is not None:
            body["section_versions"] = {self.NAME: version}
        saved = self.ok("patch", "/api/v1/themes/settings/", body)["sections"][self.NAME]
        preview = self.preview(values)
        self.timings["sidebar -> code + preview"].append((time.perf_counter() - started) * 1000)
        return saved, preview

    def save_code(self, content, expect=200, version=None):
        started = time.perf_counter()
        body = {"path": self.PATH, "content": content}
        if version is not None:
            body["base_version"] = version
        r = self.client.post("/api/v1/themes/files/", body, format="json")
        self.assertEqual(r.status_code, expect, r.content[:500])
        if expect == 200:
            self.preview(r.json()["sync"]["values"])
            self.timings["code save -> blocks + preview"].append((time.perf_counter() - started) * 1000)
        return r.json()

    def preview(self, values):
        """The customizer's request for just this section's preview HTML."""
        data = self.ok("get", "/api/v1/themes/settings/")
        data["sections"] = {self.NAME: values}
        return self.ok("post", f"/api/v1/themes/preview/?section={self.NAME}", data)["html"]

    def error_text(self, error, key):
        value = error[key]
        return str(value[0] if isinstance(value, list) else value)

    def home(self):
        r = self.client.get(f"/s/{self.store['storeSlug']}/")
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def schema_of(self, content):
        return json.loads(sync.split(content)[1])

    def with_schema(self, content, change):
        markup, schema_text, _ = sync.split(content)
        schema = json.loads(schema_text)
        change(schema)
        start = content.index("{% schema %}")
        return content[:start] + "{% schema %}\n" + json.dumps(schema, indent=2) + "\n{% endschema %}\n"

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        for kind, values in cls.timings.items():
            if values:
                print(f"\n  [{cls.NAME} sync] {kind}: average {sum(values) / len(values):.0f} ms, "
                      f"slowest {max(values):.0f} ms ({len(values)} runs)")


class FooterSyncTest(SyncedSectionMixin, APITestCase):
    NAME = "footer"
    EMAIL = "footer@example.com"
    # "change made -> preview updated", measured through the API (all tests together)
    timings = {"sidebar -> code + preview": [], "code save -> blocks + preview": []}

    def footer(self):
        return self.section()

    # ---------------------------------------------------------------- the 7 checks

    def test_1_setting_added_in_schema_appears_in_sidebar(self):
        content = self.code()
        new = self.with_schema(content, lambda s: s["settings"].append(
            {"type": "text", "id": "tagline", "label": "Tagline", "default": "Made with care"}))
        new = new.replace("<div class=\"page-width site-footer__legal\">",
                          "<p class=\"footer-tagline\">{{ section.settings.tagline }}</p>\n  <div class=\"page-width site-footer__legal\">")
        saved = self.save_code(new)
        self.assertIn("tagline", saved["sync"]["spec"]["settings"])
        footer = self.footer()
        self.assertEqual(footer["spec"]["settings"]["tagline"]["label"], "Tagline")
        self.assertEqual(footer["values"]["settings"]["tagline"], "Made with care")
        self.assertIn("Made with care", self.home())

    def test_2_sidebar_text_change_updates_code_and_preview(self):
        footer = self.footer()
        values = footer["values"]
        values["blocks"].insert(0, {"type": "text", "settings": {"heading": "About us", "text": "Hand-made in Jaipur"}})
        before = self.code()
        saved, preview = self.save_values(values, footer["version"])
        self.assertIn("Hand-made in Jaipur", preview)
        self.assertIsNone(saved["conflict"])

        after = self.code()
        self.assertIn('"Hand-made in Jaipur"', after)
        # Only the schema's default blocks changed: the markup above it is untouched
        self.assertEqual(sync.split(before)[0].rstrip("\n"), sync.split(after)[0].rstrip("\n"))
        self.assertEqual(self.schema_of(after)["default"]["blocks"][0]["settings"]["text"], "Hand-made in Jaipur")
        self.assertIn("Hand-made in Jaipur", self.home())

        # A section setting goes into its "default"
        values["settings"]["copyright"] = "© Footer Store Ltd"
        self.save_values(values)
        copyright_field = next(f for f in self.schema_of(self.code())["settings"] if f["id"] == "copyright")
        self.assertEqual(copyright_field["default"], "© Footer Store Ltd")

    def test_3_deleting_a_block_updates_code_and_preview(self):
        values = self.footer()["values"]
        values["blocks"] = [
            {"type": "text", "settings": {"heading": "One", "text": "First block"}},
            {"type": "newsletter", "settings": {"heading": "Join us", "text": "", "button_label": "Go"}},
        ]
        self.save_values(values)
        self.assertIn("Join us", self.home())

        values["blocks"].pop(1)
        _, preview = self.save_values(values)
        self.assertNotIn("Join us", preview)
        self.assertEqual([b["type"] for b in self.schema_of(self.code())["default"]["blocks"]], ["text"])
        self.assertNotIn("Join us", self.home())

    def test_4_editing_footer_markup_updates_preview(self):
        content = self.code().replace('<footer class="site-footer"', '<footer data-edited="yes" class="site-footer"')
        self.save_code(content)
        self.assertIn('data-edited="yes"', self.preview(self.footer()["values"]))
        self.assertIn('data-edited="yes"', self.home())

    def test_5_reordering_blocks_updates_code_order_and_preview(self):
        values = self.footer()["values"]
        values["blocks"] = [
            {"type": "text", "settings": {"heading": "Alpha", "text": "a"}},
            {"type": "text", "settings": {"heading": "Beta", "text": "b"}},
        ]
        self.save_values(values)
        values["blocks"].reverse()
        _, preview = self.save_values(values)
        self.assertLess(preview.index("Beta"), preview.index("Alpha"))
        headings = [b["settings"]["heading"] for b in self.schema_of(self.code())["default"]["blocks"]]
        self.assertEqual(headings, ["Beta", "Alpha"])
        home = self.home()
        self.assertLess(home.index("Beta"), home.index("Alpha"))

    def test_6_broken_schema_is_refused_and_nothing_is_lost(self):
        values = self.footer()["values"]
        values["blocks"].insert(0, {"type": "text", "settings": {"heading": "Keep me", "text": "Still here"}})
        self.save_values(values)
        good = self.code()

        broken = good.replace('"max_blocks": 8,', '"max_blocks": 8')
        error = self.save_code(broken, expect=400)
        self.assertIn("schema is not valid JSON", error["content"][0] if isinstance(error["content"], list) else error["content"])
        line = int(error["line"][0] if isinstance(error["line"], list) else error["line"])
        self.assertEqual(broken.splitlines()[line - 1].strip()[:12], '"settings": ')

        bad_liquid = good.replace("{%- endcase -%}", "")
        error = self.save_code(bad_liquid, expect=400)
        self.assertIn("Line", str(error["content"]))

        # The previous version keeps working, with its values
        self.assertEqual(self.code(), good)
        self.assertIn("Still here", self.home())
        self.assertEqual(self.footer()["values"]["blocks"][0]["settings"]["heading"], "Keep me")

    def test_7_two_quick_edits_both_show(self):
        footer = self.footer()
        values = footer["values"]
        values["settings"]["copyright"] = "First edit"
        first, _ = self.save_values(values, footer["version"])
        values["blocks"].insert(0, {"type": "text", "settings": {"heading": "Second edit", "text": "x"}})
        second, preview = self.save_values(values, first["version"])
        self.assertIsNone(second["conflict"])
        for html in (preview, self.home()):
            self.assertIn("First edit", html)
            self.assertIn("Second edit", html)
        code = self.code()
        self.assertIn('"First edit"', code)
        self.assertIn('"Second edit"', code)

    # ---------------------------------------------------------------- safety

    def test_unsafe_links_are_refused(self):
        values = self.footer()["values"]
        values["blocks"] = [{"type": "social", "settings": {"instagram": "javascript:alert(1)"}}]
        self.ok("patch", "/api/v1/themes/settings/", {"sections": {"footer": values}}, expect=400)
        values["blocks"] = [{"type": "links", "settings": {"menu": "Evil | javascript:alert(1)"}}]
        self.ok("patch", "/api/v1/themes/settings/", {"sections": {"footer": values}}, expect=400)
        # Text is escaped
        values["blocks"] = [{"type": "text", "settings": {"heading": "<script>x()</script>", "text": ""}}]
        self.ok("patch", "/api/v1/themes/settings/", {"sections": {"footer": values}})
        self.assertNotIn("<script>x()</script>", self.home())

    def test_conflict_is_reported_and_history_restores(self):
        footer = self.footer()
        old_version = footer["version"]
        content = self.code().replace('<footer class="site-footer"', '<footer data-code="1" class="site-footer"')
        self.save_code(content)

        # The customizer still had the old version: its change wins, and it is told
        values = footer["values"]
        values["settings"]["copyright"] = "From the customizer"
        saved, _ = self.save_values(values, old_version)
        self.assertIn("code editor changed the footer", saved["conflict"])
        self.assertIn('data-code="1"', self.code())  # the code edit was kept as well (different lines)

        history = self.ok("get", "/api/v1/themes/sections/footer/history/")["history"]
        self.assertGreaterEqual(len(history), 2)
        restored = self.ok("post", "/api/v1/themes/sections/footer/history/", {"index": len(history) - 1})
        self.assertGreater(restored["sync"]["version"], saved["version"])
        self.assertNotIn('data-code="1"', self.code())

    def test_history_keeps_20_versions(self):
        content = self.code()
        for i in range(25):
            self.save_code(content.replace('<footer class="site-footer"', f'<footer data-v="{i}" class="site-footer"'))
        history = self.ok("get", "/api/v1/themes/sections/footer/history/")["history"]
        self.assertEqual(len(history), sync.HISTORY_LIMIT)

    def test_other_stores_cannot_see_or_change_it(self):
        self.save_code(self.code().replace('<footer class="site-footer"', '<footer data-owner="a" class="site-footer"'))
        _, other = self.sign_up("other@example.com", "Other Store")
        files = self.ok("get", "/api/v1/themes/files/")["files"]
        self.assertNotIn('data-owner="a"', next(f["content"] for f in files if f["path"] == self.PATH))

    def test_events_reach_the_store_only(self):
        seq = sync.EVENTS.last_seq
        values = self.footer()["values"]
        values["settings"]["copyright"] = "Evented"
        self.save_values(values)
        events, _ = sync.EVENTS.wait(self.store_db_id(), seq, 0.1)
        self.assertTrue(any(p["values"]["settings"]["copyright"] == "Evented" for _, p in events))
        self.assertEqual(sync.EVENTS.wait("someone-else", seq, 0.05)[0], [])

    def test_event_stream_sends_section_events(self):
        from unittest import mock

        from .views import ThemeEventsView

        seq = sync.EVENTS.last_seq
        values = self.footer()["values"]
        values["settings"]["copyright"] = "Streamed"
        self.save_values(values)
        with mock.patch.object(ThemeEventsView, "STREAM_SECONDS", 0.3):
            r = self.client.get(f"/api/v1/themes/events/?after={seq}")
            self.assertEqual(r["Content-Type"], "text/event-stream")
            body = b"".join(r.streaming_content).decode()
        self.assertIn("event: hello", body)
        self.assertIn("event: section", body)
        self.assertIn("Streamed", body)
        # The stream left the test's database connection usable
        self.assertEqual(self.footer()["values"]["settings"]["copyright"], "Streamed")

    def store_db_id(self):
        from apps.tenancy.models import Store
        return Store.objects.get(store_id=self.store["storeId"]).pk

    def test_other_sections_are_unchanged(self):
        settings = self.ok("get", "/api/v1/themes/settings/")
        types = {s["type"] for s in settings["templates"]["index"]["sections"].values()}
        self.assertNotIn("footer", types)
        schema = self.ok("get", "/api/v1/themes/schema/")
        self.assertNotIn("footer", schema["sections"])


class HeaderSyncTest(SyncedSectionMixin, APITestCase):
    NAME = "header"
    EMAIL = "header@example.com"
    timings = {"sidebar -> code + preview": [], "code save -> blocks + preview": []}

    def links(self, *pairs):
        return [{"type": "link", "settings": {"label": label, "url": url}} for label, url in pairs]

    def header_html(self, html):
        start = html.index('<header class="site-header"')
        return html[start:html.index("</header>", start)]

    # ---------------------------------------------------------------- the 7 checks

    def test_1_setting_added_in_schema_appears_in_sidebar(self):
        new = self.with_schema(self.code(), lambda s: s["settings"].append(
            {"type": "text", "id": "tagline", "label": "Tagline", "default": "Free delivery"}))
        new = new.replace('    </a>\n    {%- if s.show_nav -%}',
                          '    </a>\n    <span class="header-tagline">{{ section.settings.tagline }}</span>\n    {%- if s.show_nav -%}')
        saved = self.save_code(new)
        self.assertIn("tagline", saved["sync"]["spec"]["settings"])
        header = self.section()
        self.assertEqual(header["spec"]["settings"]["tagline"]["label"], "Tagline")
        self.assertEqual(header["values"]["settings"]["tagline"], "Free delivery")
        self.assertIn('<span class="header-tagline">Free delivery</span>', self.home())

    def test_2_sidebar_text_change_updates_code_and_preview(self):
        header = self.section()
        values = header["values"]
        values["settings"]["logo_text"] = "Maison Lune"
        values["blocks"] = self.links(("New in", "/collections/all"))
        before = self.code()
        saved, preview = self.save_values(values, header["version"])
        self.assertIsNone(saved["conflict"])
        self.assertIn("Maison Lune", preview)
        self.assertIn(">New in</a>", preview)

        after = self.code()
        self.assertEqual(sync.split(before)[0].rstrip("\n"), sync.split(after)[0].rstrip("\n"))  # markup untouched
        schema = self.schema_of(after)
        self.assertEqual(next(f for f in schema["settings"] if f["id"] == "logo_text")["default"], "Maison Lune")
        self.assertEqual(schema["default"]["blocks"][0]["settings"]["label"], "New in")
        home = self.home()
        self.assertIn("Maison Lune", self.header_html(home))
        self.assertIn(f'/s/{self.store["storeSlug"]}/collections/all">New in</a>', home)

    def test_3_deleting_a_block_updates_code_and_preview(self):
        values = self.section()["values"]
        values["blocks"] = self.links(("Shop", "/collections/all"), ("Journal", "/blog"))
        self.save_values(values)
        self.assertIn(">Journal</a>", self.home())

        values["blocks"].pop(1)
        _, preview = self.save_values(values)
        self.assertNotIn("Journal", preview)
        self.assertEqual([b["settings"]["label"] for b in self.schema_of(self.code())["default"]["blocks"]], ["Shop"])
        self.assertNotIn(">Journal</a>", self.home())

    def test_4_editing_header_markup_updates_preview(self):
        content = self.code().replace('<div class="page-width site-header__inner">',
                                      '<div class="page-width site-header__inner" data-edited="yes">')
        self.save_code(content)
        self.assertIn('data-edited="yes"', self.preview(self.section()["values"]))
        self.assertIn('data-edited="yes"', self.home())

    def test_5_reordering_blocks_updates_code_order_and_preview(self):
        values = self.section()["values"]
        values["blocks"] = self.links(("Alpha", "/a"), ("Beta", "/b"))
        self.save_values(values)
        values["blocks"].reverse()
        _, preview = self.save_values(values)
        self.assertLess(preview.index(">Beta<"), preview.index(">Alpha<"))
        self.assertEqual([b["settings"]["label"] for b in self.schema_of(self.code())["default"]["blocks"]], ["Beta", "Alpha"])
        home = self.home()
        self.assertLess(home.index(">Beta<"), home.index(">Alpha<"))

    def test_6_broken_schema_is_refused_and_nothing_is_lost(self):
        values = self.section()["values"]
        values["blocks"] = self.links(("Keep me", "/collections/all"))
        self.save_values(values)
        good = self.code()

        broken = good.replace('"max_blocks": 12,', '"max_blocks": 12')
        error = self.save_code(broken, expect=400)
        self.assertIn("schema is not valid JSON", self.error_text(error, "content"))
        line = int(self.error_text(error, "line"))
        self.assertEqual(broken.splitlines()[line - 1].strip()[:12], '"settings": ')

        error = self.save_code(good.replace("{%- endfor -%}", "", 1), expect=400)
        self.assertIn("Line", self.error_text(error, "content"))

        self.assertEqual(self.code(), good)
        self.assertIn(">Keep me</a>", self.home())
        self.assertEqual(self.section()["values"]["blocks"][0]["settings"]["label"], "Keep me")

    def test_7_two_quick_edits_both_show(self):
        header = self.section()
        values = header["values"]
        values["settings"]["logo_text"] = "First edit"
        first, _ = self.save_values(values, header["version"])
        values["blocks"] = self.links(("Second edit", "/collections/all"))
        second, preview = self.save_values(values, first["version"])
        self.assertIsNone(second["conflict"])
        for html in (preview, self.home()):
            self.assertIn("First edit", html)
            self.assertIn("Second edit", html)
        code = self.code()
        self.assertIn('"First edit"', code)
        self.assertIn('"Second edit"', code)

    # ---------------------------------------------------------------- header specifics

    def test_sticky_header_is_not_wrapped_in_the_preview(self):
        # The customizer marks the <header> itself: a wrapping <div> would stop position: sticky
        preview = self.preview(self.section()["values"])
        self.assertTrue(preview.startswith('<header class="site-header" data-tss-section="header" data-tss-fixed'), preview[:120])
        self.assertIn('data-sticky="true"', preview)
        page = self.ok("post", "/api/v1/themes/preview/", self.ok("get", "/api/v1/themes/settings/"))["html"]
        self.assertIn('<header class="site-header" data-tss-section="header"', page)
        self.assertNotIn('<div data-tss-section="header"', page)
        # The store's own page has no editor markers
        self.assertNotIn("data-tss-section", self.home())

        # Code without {{ section.attrs }} is wrapped instead, so the customizer can still select it
        self.save_code(self.code().replace("{{ section.attrs }}", ""))
        self.assertTrue(self.preview(self.section()["values"]).startswith('<div data-tss-section="header"'))

    def test_preview_marks_the_header_like_the_footer(self):
        """The preview's click handler selects [data-tss-section]; the customizer looks the id up as a synced section."""
        import re

        values = self.section()["values"]
        values["blocks"] = self.links(("Shop", "/collections/all"), ("Journal", "/blog"))
        self.save_values(values)
        page = self.ok("post", "/api/v1/themes/preview/", self.ok("get", "/api/v1/themes/settings/"))["html"]
        synced = self.ok("get", "/api/v1/themes/sections/")
        for name, label in (("header", "Header"), ("footer", "Footer")):
            tag = re.search(rf'<\w+[^>]*data-tss-section="{name}"[^>]*>', page)
            self.assertIsNotNone(tag, name)
            self.assertIn("data-tss-fixed", tag.group(0))
            self.assertIn(f'data-tss-label="{label}"', tag.group(0))
            self.assertIn(name, synced)  # the id the click sends is a synced section the customizer can open
        # Each menu link is a block of the header: a click on it selects the header and that link
        header = page[page.index('data-tss-section="header"'):page.index("</header>")]
        self.assertIn('data-tss-block="0" data-tss-label="Menu link">Shop</a>', header)
        self.assertIn('data-tss-block="1" data-tss-label="Menu link">Journal</a>', header)
        # Edit mode stops every click in the page before links can navigate
        self.assertIn("e.preventDefault();", page)

    def test_automatic_menu_when_there_are_no_links(self):
        for name in ("Autumn", "Basics", "Coats"):
            self.ok("post", "/api/v1/catalog/collections/", {"name": name, "status": "active"}, expect=201)
        values = self.section()["values"]
        self.assertEqual(values["blocks"], [])
        values["settings"]["collections"] = 2
        _, preview = self.save_values(values)
        nav = self.header_html(self.home())
        self.assertIn(">Home</a>", nav)
        self.assertIn(">Shop all</a>", nav)
        shown = [name for name in ("Autumn", "Basics", "Coats") if f">{name}</a>" in nav]
        self.assertEqual(shown, ["Autumn", "Basics"])
        # The fast preview path reads the collections the same way
        self.assertEqual(shown, [name for name in ("Autumn", "Basics", "Coats") if f">{name}</a>" in preview])

    def test_existing_header_settings_are_copied_into_the_section(self):
        from .services import get_store_theme
        from apps.tenancy.models import Store

        store_theme = get_store_theme(Store.objects.get(store_id=self.store["storeId"]))
        data = dict(store_theme.custom_data)
        data.pop("sections", None)  # a store from before the header was synced
        data["settings"] = {**data.get("settings", {}),
                            "header_menu": "About | /pages/about\nSale | /collections/sale",
                            "header_sticky": False, "header_border": False, "header_bg": "#112233",
                            "header_text": "#FAFAFA", "header_collections": 2, "header_logo_text": "Old Name",
                            "header_layout": "center"}
        store_theme.custom_data = data
        store_theme.save()

        values = self.section()["values"]
        self.assertEqual([b["settings"]["label"] for b in values["blocks"]], ["About", "Sale"])
        self.assertEqual(values["blocks"][1]["settings"]["url"], "/collections/sale")
        settings = values["settings"]
        self.assertEqual((settings["sticky"], settings["border"], settings["background"], settings["text_color"]),
                         (False, False, "#112233", "#FAFAFA"))
        self.assertEqual((settings["collections"], settings["logo_text"], settings["layout"]), (2, "Old Name", "center"))
        header = self.header_html(self.home())
        self.assertIn('data-sticky="false"', header)
        self.assertIn('data-layout="center"', header)
        self.assertIn("Old Name", header)

    def test_a_store_that_edited_its_header_snippet_keeps_it(self):
        from .models import ThemeFile
        from .services import get_store_theme
        from apps.tenancy.models import Store

        store = Store.objects.get(store_id=self.store["storeId"])
        theme = get_store_theme(store).theme
        # A store from before: no header section yet, and its own edited header snippet
        theme.files.filter(file_path="sections/header.liquid").delete()
        theme.files.filter(file_path="snippets/header.liquid").update(content='<header class="my-own-header">Mine</header>')
        theme.theme_data = {**theme.theme_data, "synced_sections": ["footer"]}
        theme.save()

        get_store_theme(store)
        self.assertFalse(ThemeFile.objects.filter(theme=theme, file_path="sections/header.liquid").exists())
        self.assertIn('<header class="my-own-header">Mine</header>', self.home())
        self.assertNotIn("header", self.ok("get", "/api/v1/themes/sections/"))
        self.assertIn("footer", self.ok("get", "/api/v1/themes/sections/"))

    def test_an_untouched_header_snippet_gets_the_section(self):
        from .models import ThemeFile
        from .services import get_store_theme
        from apps.tenancy.models import Store

        store = Store.objects.get(store_id=self.store["storeId"])
        theme = get_store_theme(store).theme
        theme.files.filter(file_path="sections/header.liquid").delete()
        theme.theme_data = {**theme.theme_data, "synced_sections": ["footer"]}
        theme.save()

        get_store_theme(store)
        self.assertTrue(ThemeFile.objects.filter(theme=theme, file_path="sections/header.liquid").exists())
        self.assertIn("header", self.ok("get", "/api/v1/themes/sections/"))

    def test_unsafe_links_are_refused_and_text_escaped(self):
        values = self.section()["values"]
        values["blocks"] = self.links(("Evil", "javascript:alert(1)"))
        self.ok("patch", "/api/v1/themes/settings/", {"sections": {"header": values}}, expect=400)
        values["blocks"] = self.links(("<script>x()</script>", "/collections/all"))
        values["settings"]["logo_image"] = "javascript:alert(1)"
        self.ok("patch", "/api/v1/themes/settings/", {"sections": {"header": values}}, expect=400)
        values["settings"]["logo_image"] = ""
        self.ok("patch", "/api/v1/themes/settings/", {"sections": {"header": values}})
        home = self.home()
        self.assertNotIn("<script>x()</script>", home)
        self.assertIn("&lt;script&gt;x()&lt;/script&gt;", home)

    def test_header_and_footer_are_saved_together(self):
        sections = self.ok("get", "/api/v1/themes/sections/")
        header, footer = sections["header"]["values"], sections["footer"]["values"]
        header["settings"]["logo_text"] = "Both at once"
        footer["settings"]["copyright"] = "Footer too"
        saved = self.ok("patch", "/api/v1/themes/settings/", {"sections": {"header": header, "footer": footer}})
        self.assertEqual(set(saved["sections"]), {"header", "footer"})
        home = self.home()
        self.assertIn("Both at once", home)
        self.assertIn("Footer too", home)
        self.assertLess(home.index("Both at once"), home.index("<main"))
        self.assertGreater(home.index("Footer too"), home.index("</main>"))

    def test_history_and_timing_names(self):
        content = self.code()
        for i in range(22):
            self.save_code(content.replace('<div class="page-width site-header__inner">',
                                           f'<div class="page-width site-header__inner" data-v="{i}">'))
        history = self.ok("get", "/api/v1/themes/sections/header/history/")["history"]
        self.assertEqual(len(history), sync.HISTORY_LIMIT)
        # Latest first: index 0 is the version before the current one (data-v="20"), index 5 is data-v="15"
        self.ok("post", "/api/v1/themes/sections/header/history/", {"index": 5})
        self.assertIn('data-v="15"', self.code())
        self.ok("post", "/api/v1/themes/sync-timings/", {"kind": "header: sidebar -> preview", "ms": 42}, expect=204)
        self.ok("post", "/api/v1/themes/sync-timings/", {"kind": "footer: sidebar -> preview", "ms": 42}, expect=204)
        self.ok("post", "/api/v1/themes/sync-timings/", {"kind": "hero: sidebar -> preview", "ms": 42}, expect=400)
        self.assertIn("header: sidebar -> preview", self.ok("get", "/api/v1/themes/sync-timings/"))

    def test_other_sections_are_unchanged(self):
        schema = self.ok("get", "/api/v1/themes/schema/")
        self.assertNotIn("header", schema["sections"])
        for name in ("hero", "slideshow", "rich-text", "product-grid", "product-carousel", "main-product", "main-collection"):
            self.assertIn(name, schema["sections"])


# The first starter header (2df94aa), before the header followed Theme settings
OLDEST_STARTER_HEADER = """<header class="site-header">
  <div class="page-width site-header__inner">
    <a class="site-header__logo" href="{{ shop.url }}/">{{ shop.name }}</a>
    <nav class="site-header__nav" aria-label="Main">
      <a href="{{ shop.url }}/">Home</a>
      <a href="{{ shop.url }}/collections/all">Shop all</a>
      {% for collection in collections limit: 4 %}
        <a href="{{ collection.url }}">{{ collection.name }}</a>
      {% endfor %}
    </nav>
  </div>
</header>
"""


class HeaderMigrationTest(StoreClientMixin, APITestCase):
    """Which existing themes get sections/header.liquid (the original-snippet check)."""

    def setUp(self):
        from .services import get_store_theme
        from apps.tenancy.models import Store

        _, store = self.sign_up("migrate@example.com", "Migrate Store")
        self.store = Store.objects.get(store_id=store["storeId"])
        self.theme = get_store_theme(self.store).theme

    def old_theme(self, snippet, checked=("footer",)):
        """A theme from before the header was synced: no section file, this header snippet."""
        self.theme.files.filter(file_path="sections/header.liquid").delete()
        self.theme.files.filter(file_path="snippets/header.liquid").update(content=snippet)
        self.theme.theme_data = {**self.theme.theme_data, "synced_sections": list(checked)}
        self.theme.save()

    def upgrade(self):
        from .models import ThemeFile
        from .services import get_store_theme

        get_store_theme(self.store)
        return ThemeFile.objects.filter(theme=self.theme, file_path="sections/header.liquid").count()

    def test_oldest_untouched_starter_header_gets_the_section(self):
        self.assertEqual(hashlib.sha256(OLDEST_STARTER_HEADER.encode()).hexdigest(),
                         "6e36e4b3429052522d9242b9f5cbb83c98ae509b028f38bc0381169ff4c9dec1")
        self.old_theme(OLDEST_STARTER_HEADER)
        self.assertEqual(self.upgrade(), 1)

    def test_oldest_untouched_starter_header_with_crlf_line_endings_gets_the_section(self):
        self.old_theme(OLDEST_STARTER_HEADER.replace("\n", "\r\n"))
        self.assertEqual(self.upgrade(), 1)

    def test_an_edited_old_header_keeps_its_snippet(self):
        # The oldest starter plus one line the store added (a Blog link)
        edited = OLDEST_STARTER_HEADER.replace(
            "      {% endfor %}\n", "      {% endfor %}\n      <a href=\"{{ shop.url }}/blog\">Blog</a>\n")
        self.old_theme(edited)
        self.assertEqual(self.upgrade(), 0)
        r = self.client.get(f"/s/{self.store.store_slug}/")
        self.assertIn(">Blog</a>", r.content.decode())

    def test_a_theme_checked_with_fewer_known_originals_is_checked_again(self):
        # Checked before (revision 1) when its untouched snippet was not recognised
        from .services import starter_files

        self.old_theme(starter_files()["snippets/header.liquid"], checked=("footer", "header"))
        self.assertEqual(self.upgrade(), 1)
        self.assertEqual(self.theme.__class__.objects.get(pk=self.theme.pk).theme_data["synced_sections"]["header"],
                         sync.ORIGINALS_REVISION)

    def test_checking_again_never_adds_a_second_file(self):
        self.old_theme(OLDEST_STARTER_HEADER)
        self.assertEqual(self.upgrade(), 1)
        self.theme.theme_data = {**self.theme.__class__.objects.get(pk=self.theme.pk).theme_data, "synced_sections": ["footer"]}
        self.theme.save()
        self.assertEqual(self.upgrade(), 1)
        from .models import ThemeFile
        self.assertEqual(ThemeFile.objects.filter(theme=self.theme, file_path="sections/footer.liquid").count(), 1)


class HomeTemplateSyncTest(StoreClientMixin, APITestCase):
    """templates/index.liquid lists the Home page's sections; the customizer's list follows it, both ways."""

    PATH = "templates/index.liquid"
    timings = {"sidebar -> code + preview": [], "code save -> list + preview": []}

    def setUp(self):
        self.token, self.store = self.sign_up("home@example.com", "Home Store")
        # Three sections that are easy to find on the page
        self.ok("put", "/api/v1/themes/settings/", {"templates": {"index": {
            "order": ["alpha", "beta", "gamma"],
            "sections": {sid: {"type": "rich-text", "settings": {"heading": sid.title()}}
                         for sid in ("alpha", "beta", "gamma")},
        }}})

    # ---------------------------------------------------------------- helpers

    def code(self):
        files = self.ok("get", "/api/v1/themes/files/")["files"]
        return next(f["content"] for f in files if f["path"] == self.PATH)

    def index(self):
        return self.ok("get", "/api/v1/themes/settings/")["templates"]["index"]

    def lines(self, content=None):
        return [t["id"] for t in sync.template_tags(content if content is not None else self.code())]

    def home(self):
        r = self.client.get(f"/s/{self.store['storeSlug']}/")
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def page_order(self, html, names=("Alpha", "Beta", "Gamma", "Delta")):
        found = [(html.find(f">{n}<"), n) for n in names if f">{n}<" in html]
        return [n for _, n in sorted(found)]

    def reorder(self, order, version=None):
        started = time.perf_counter()
        body = {"templates": {"index": {"order": order}}, "client": "test"}
        if version is not None:
            body["section_versions"] = {"index": version}
        reply = self.ok("patch", "/api/v1/themes/settings/", body)
        data = self.ok("get", "/api/v1/themes/settings/")
        preview = self.ok("post", "/api/v1/themes/preview/", data)["html"]
        self.timings["sidebar -> code + preview"].append((time.perf_counter() - started) * 1000)
        return reply, preview

    def save_code(self, content, expect=200, version=None):
        started = time.perf_counter()
        body = {"path": self.PATH, "content": content}
        if version is not None:
            body["base_version"] = version
        r = self.client.post("/api/v1/themes/files/", body, format="json")
        self.assertEqual(r.status_code, expect, r.content[:500])
        if expect == 200:
            self.ok("post", "/api/v1/themes/preview/", self.ok("get", "/api/v1/themes/settings/"))
            self.timings["code save -> list + preview"].append((time.perf_counter() - started) * 1000)
        return r.json()

    def error_text(self, error, key):
        value = error[key]
        return str(value[0] if isinstance(value, list) else value)

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        for kind, values in cls.timings.items():
            if values:
                print(f"\n  [index sync] {kind}: average {sum(values) / len(values):.0f} ms, "
                      f"slowest {max(values):.0f} ms ({len(values)} runs)")

    # ---------------------------------------------------------------- both ways

    def test_the_starter_template_becomes_section_lines(self):
        code = self.code()
        self.assertNotIn("{{ sections }}", code)
        self.assertEqual(self.lines(code), ["alpha", "beta", "gamma"])
        self.assertIn("{% section 'beta', type: 'rich-text' %}", code)
        synced = self.ok("get", "/api/v1/themes/sections/")
        self.assertEqual(synced["index"]["kind"], "template")
        self.assertEqual(synced["index"]["template"]["order"], ["alpha", "beta", "gamma"])
        # A new store's own default page was turned into lines too
        _, other = self.sign_up("fresh@example.com", "Fresh Store")
        files = self.ok("get", "/api/v1/themes/files/")["files"]
        fresh = next(f["content"] for f in files if f["path"] == self.PATH)
        self.assertEqual(self.lines(fresh), ["slideshow", "featured", "carousel", "hero", "about"])

    def test_sidebar_reorder_rewrites_only_the_section_lines(self):
        before = self.code()
        reply, preview = self.reorder(["gamma", "alpha", "beta"], version=self.ok("get", "/api/v1/themes/sections/")["index"]["version"])
        self.assertIsNone(reply["templates"]["index"]["conflict"])
        after = self.code()
        self.assertEqual(self.lines(after), ["gamma", "alpha", "beta"])
        changed = [(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
        self.assertEqual(len(before.splitlines()), len(after.splitlines()))
        self.assertEqual(len(changed), 3)  # the three section lines; the comment and everything else stay
        self.assertTrue(all("{% section" in a and "{% section" in b for a, b in changed))
        self.assertEqual(self.page_order(preview), ["Gamma", "Alpha", "Beta"])
        self.assertEqual(self.page_order(self.home()), ["Gamma", "Alpha", "Beta"])

    def test_code_reorder_rebuilds_the_sidebar_list(self):
        code = self.code()
        moved = code.replace("{% section 'alpha', type: 'rich-text' %}\n", "").replace(
            "{% section 'gamma', type: 'rich-text' %}\n", "{% section 'gamma', type: 'rich-text' %}\n{% section 'alpha', type: 'rich-text' %}\n")
        saved = self.save_code(moved)
        self.assertEqual(saved["sync"]["template"]["order"], ["beta", "gamma", "alpha"])
        self.assertEqual(self.index()["order"], ["beta", "gamma", "alpha"])
        self.assertEqual(self.page_order(self.home()), ["Beta", "Gamma", "Alpha"])

    def test_reordering_never_loses_values(self):
        self.ok("patch", "/api/v1/themes/settings/", {"templates": {"index": {"sections": {
            "beta": {"type": "rich-text", "hidden": False, "settings": {"heading": "Beta", "text": "<p>Kept text</p>"},
                     "blocks": [{"type": "button", "settings": {"label": "Kept button", "link": "/collections/all"}}]},
        }}}})
        self.reorder(["beta", "gamma", "alpha"])
        self.save_code(self.code().replace("{% section 'beta', type: 'rich-text' %}\n", "") + "{% section 'beta', type: 'rich-text' %}\n")
        beta = self.index()["sections"]["beta"]
        self.assertEqual(self.index()["order"], ["gamma", "alpha", "beta"])
        self.assertEqual(beta["settings"]["text"], "<p>Kept text</p>")
        self.assertEqual([b["settings"]["label"] for b in beta["blocks"]], ["Kept button"])

    def test_adding_removing_and_hiding_in_code(self):
        self.ok("patch", "/api/v1/themes/settings/", {"templates": {"index": {"sections": {
            "gamma": {"type": "rich-text", "settings": {"heading": "Gamma", "text": "<p>Gamma text</p>"}},
        }}}})
        code = self.code()
        # Add a new section (new id + type), remove gamma, hide beta
        code = code.replace("{% section 'gamma', type: 'rich-text' %}\n", "{% section 'delta', type: 'rich-text' %}\n")
        code = code.replace("{% section 'beta', type: 'rich-text' %}", "{% section 'beta', type: 'rich-text', hidden: true %}")
        self.save_code(code)
        index = self.index()
        self.assertEqual(index["order"], ["alpha", "beta", "delta"])
        self.assertTrue(index["sections"]["beta"]["hidden"])
        self.assertNotIn("gamma", index["sections"])
        self.assertEqual(index["sections"]["delta"]["type"], "rich-text")
        home = self.home()
        self.assertNotIn(">Beta<", home)
        self.assertNotIn(">Gamma<", home)

        # Putting gamma's line back (even without a type) brings its values back
        self.save_code(self.code() + "{% section 'gamma' %}\n")
        gamma = self.index()["sections"]["gamma"]
        self.assertEqual((gamma["settings"]["heading"], gamma["settings"]["text"]), ("Gamma", "<p>Gamma text</p>"))

    def test_sidebar_hide_add_and_remove_update_the_lines(self):
        index = self.index()
        sections = {"beta": dict(index["sections"]["beta"], hidden=True),
                    "new-one": {"type": "hero", "settings": {}}, "gamma": None}
        self.ok("patch", "/api/v1/themes/settings/", {"templates": {"index": {
            "order": ["alpha", "beta", "new-one"], "sections": sections}}})
        code = self.code()
        self.assertEqual(self.lines(code), ["alpha", "beta", "new-one"])
        self.assertIn("{% section 'beta', type: 'rich-text', hidden: true %}", code)
        self.assertIn("{% section 'new-one', type: 'hero' %}", code)
        self.assertNotIn("'gamma'", code)

    def test_markup_around_the_lines_is_kept(self):
        code = self.code().replace("{% section 'beta', type: 'rich-text' %}",
                                   "<div class=\"home-band\">\n{% section 'beta', type: 'rich-text' %}\n</div>")
        self.save_code(code)
        self.reorder(["gamma", "beta", "alpha"])
        after = self.code()
        self.assertIn('<div class="home-band">', after)
        self.assertEqual(self.lines(after), ["gamma", "beta", "alpha"])
        home = self.home()
        band = home[home.index('<div class="home-band">'):]
        self.assertIn(">Beta<", band[:band.index("</div>\n") + 2000])
        self.assertEqual(self.page_order(home), ["Gamma", "Beta", "Alpha"])

    def test_unsaved_preview_shows_the_order_being_edited(self):
        data = self.ok("get", "/api/v1/themes/settings/")
        data["templates"]["index"]["order"] = ["beta", "alpha", "gamma"]
        preview = self.ok("post", "/api/v1/themes/preview/", data)["html"]
        self.assertEqual(self.page_order(preview), ["Beta", "Alpha", "Gamma"])
        self.assertEqual(self.lines(), ["alpha", "beta", "gamma"])  # nothing saved

    def test_templates_json_save_updates_the_lines(self):
        files = self.ok("get", "/api/v1/themes/files/")["files"]
        data = json.loads(next(f["content"] for f in files if f["path"] == "templates/index.json"))
        data["order"] = ["beta", "gamma", "alpha"]
        self.ok("post", "/api/v1/themes/files/", {"path": "templates/index.json", "content": json.dumps(data)})
        self.assertEqual(self.lines(), ["beta", "gamma", "alpha"])

    # ---------------------------------------------------------------- invalid code

    def test_invalid_code_is_refused_with_its_line_and_nothing_changes(self):
        good = self.code()
        cases = [
            (good + "{% section 'nope' %}\n", "there is no section \"nope\""),
            (good + "{% section 'alpha' %}\n", "listed twice"),
            (good + "{% section 'x1', type: 'main-product' %}\n", "can't be used on this page"),
            (good + "{% section 'beta', type: 'hero' %}\n", "listed twice"),
            (good.replace("{% section 'alpha', type: 'rich-text' %}", "{% section 'alpha', type: 'hero' %}"), "is a Rich text section"),
            (good + "{% section 'Bad Id' %}\n", "not a valid section id"),
            (good + "{% section 'x2', colour: 'red' %}\n", "Unknown option"),
            (good + "{% if %}\n", "Liquid error"),
        ]
        for content, message in cases:
            error = self.save_code(content, expect=400)
            self.assertIn(message, self.error_text(error, "content"), content[-60:])
            line = int(self.error_text(error, "line"))
            self.assertGreater(line, 1)
            self.assertIn(self.error_text(error, "content").split(":")[0], (f"Line {line}",))
        self.assertEqual(self.code(), good)
        self.assertEqual(self.index()["order"], ["alpha", "beta", "gamma"])
        self.assertEqual(self.page_order(self.home()), ["Alpha", "Beta", "Gamma"])

    # ---------------------------------------------------------------- safety

    def test_each_store_has_its_own_template(self):
        self.reorder(["gamma", "beta", "alpha"])
        _, other = self.sign_up("other-home@example.com", "Other Home")
        self.assertEqual(self.lines(), ["slideshow", "featured", "carousel", "hero", "about"])
        self.assertEqual(self.index()["order"], ["slideshow", "featured", "carousel", "hero", "about"])

    def test_history_restore_and_conflict(self):
        version = self.ok("get", "/api/v1/themes/sections/")["index"]["version"]
        # Edit code moves gamma first
        code = self.code()
        self.save_code(code.replace("{% section 'gamma', type: 'rich-text' %}\n", "").replace(
            "{% section 'alpha'", "{% section 'gamma', type: 'rich-text' %}\n{% section 'alpha'"))
        # The customizer still had the old version: its change wins, and it is told
        reply, _ = self.reorder(["beta", "alpha", "gamma"], version=version)
        self.assertIn("code editor changed the home page", reply["templates"]["index"]["conflict"])
        self.assertEqual(self.lines(), ["beta", "alpha", "gamma"])

        history = self.ok("get", "/api/v1/themes/sections/index/history/")["history"]
        self.assertGreaterEqual(len(history), 2)
        self.ok("post", "/api/v1/themes/sections/index/history/", {"index": 0})  # the code editor's version
        self.assertEqual(self.lines(), ["gamma", "alpha", "beta"])
        self.assertEqual(self.index()["order"], ["gamma", "alpha", "beta"])

        for i in range(22):
            self.reorder(["alpha", "beta", "gamma"] if i % 2 else ["gamma", "beta", "alpha"])
            self.save_code(self.code() + f"{{%- comment -%}} v{i} {{%- endcomment -%}}\n")
        self.assertEqual(len(self.ok("get", "/api/v1/themes/sections/index/history/")["history"]), sync.HISTORY_LIMIT)

    def test_header_and_footer_stay_fixed_around_the_page(self):
        home = self.home()
        main = home.index("<main")
        self.assertLess(home.index('<header class="site-header"'), main)
        self.assertGreater(home.index('<footer class="site-footer"'), home.index("</main>"))
        for name in self.page_order(home):
            self.assertTrue(main < home.index(f">{name}<") < home.index("</main>"))
        synced = self.ok("get", "/api/v1/themes/sections/")
        self.assertEqual({synced["header"]["kind"], synced["footer"]["kind"]}, {"section"})
        self.ok("post", "/api/v1/themes/sync-timings/", {"kind": "index: sidebar -> preview", "ms": 12}, expect=204)

    def test_an_edited_old_template_keeps_working_unsynced(self):
        from .models import ThemeFile
        from .services import get_store_theme
        from apps.tenancy.models import Store

        store = Store.objects.get(store_id=self.store["storeId"])
        theme = get_store_theme(store).theme
        theme.files.filter(file_path=self.PATH).update(content='<div class="my-home">{{ sections }}</div>\n')
        theme.theme_data = {**theme.theme_data, "synced_sections": {"footer": 2, "header": 2}}
        theme.save()
        get_store_theme(store)
        self.assertEqual(ThemeFile.objects.get(theme=theme, file_path=self.PATH).content, '<div class="my-home">{{ sections }}</div>\n')
        self.assertNotIn("index", self.ok("get", "/api/v1/themes/sections/"))
        self.reorder(["gamma", "beta", "alpha"])
        home = self.home()
        self.assertIn('<div class="my-home">', home)
        self.assertEqual(self.page_order(home), ["Gamma", "Beta", "Alpha"])
        # Saving such a file is an ordinary save
        self.save_code('<section class="mine">{{ sections }}</section>\n')
        self.assertIn('<section class="mine">', self.home())
