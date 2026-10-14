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


CARD = "snippets/product-card.liquid"


class ProductCardSyncTest(StoreClientMixin, APITestCase):
    """
    snippets/product-card.liquid (Edit code) and the product list settings (Customize) together:
    the code decides the card's markup, the settings decide what it shows; neither overwrites the other.
    """

    def setUp(self):
        self.token, self.store = self.sign_up("cards@example.com", "Card Store")
        self.ok("post", "/api/v1/catalog/products/", {
            "title": "Linen shirt", "sku": "SKU-LINEN", "status": "active", "price": "25",
            "vendor": "Acme", "tags": ["summer"],
        }, expect=201)

    def files(self):
        return {f["path"]: f["content"] for f in self.ok("get", "/api/v1/themes/files/")["files"]}

    def page(self, path=""):
        r = self.client.get(f"/s/{self.store['storeSlug']}/{path}")
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def save_card(self, content, expect=200):
        r = self.client.post("/api/v1/themes/files/", {"path": CARD, "content": content}, format="json")
        self.assertEqual(r.status_code, expect, r.content[:500])
        return r.json()

    def grid_settings(self, **changes):
        data = self.ok("get", "/api/v1/themes/settings/")
        section = data["templates"]["index"]["sections"]["featured"]
        section["settings"].update(changes)
        self.ok("patch", "/api/v1/themes/settings/", {"templates": {"index": {
            "order": data["templates"]["index"]["order"], "sections": {"featured": section},
        }}})

    def test_code_save_shows_on_store_preview_and_after_reload(self):
        edited = self.files()[CARD].replace(
            '<span class="product-card__title">', '<span class="card-mark">NEW</span><span class="product-card__title">'
        )
        self.save_card(edited)
        self.assertEqual(self.files()[CARD], edited)  # persisted: what a reload of the editor shows
        self.assertIn("card-mark", self.page())  # home: grid + carousel
        self.assertIn("card-mark", self.page("collections/all"))  # collection page
        data = self.ok("get", "/api/v1/themes/settings/")
        self.assertIn("card-mark", self.ok("post", "/api/v1/themes/preview/", data)["html"])  # customizer preview

    def test_settings_change_the_card_and_keep_custom_code(self):
        edited = self.files()[CARD].replace(
            '<span class="product-card__title">', '<span class="card-mark">NEW</span><span class="product-card__title">'
        )
        self.save_card(edited)

        before = self.page()
        self.assertNotIn('class="product-card__vendor"', before)
        self.grid_settings(show_vendor=True, show_rating=True, show_tags=True,
                           show_add_to_cart=True, image_ratio="portrait", gap=10)
        html = self.page()
        for marker in ('class="product-card__vendor"', 'class="product-card__tags"', "product-card__cta\"",
                       "product-card__media product-card__media--portrait", 'class="card-mark"'):
            self.assertIn(marker, html)
        # Price off: the grid's cards have no price (the carousel still shows its own)
        prices = html.count('<span class="price">')
        self.grid_settings(show_price=False)
        self.assertLess(self.page().count('<span class="price">'), prices)
        # Persisted on both sides: settings saved, code untouched
        featured = self.ok("get", "/api/v1/themes/settings/")["templates"]["index"]["sections"]["featured"]
        self.assertTrue(featured["settings"]["show_vendor"])
        self.assertEqual(featured["settings"]["image_ratio"], "portrait")
        self.assertEqual(self.files()[CARD], edited)

    def test_product_page_preview_shows_the_clicked_product(self):
        other = self.ok("post", "/api/v1/catalog/products/", {
            "title": "Wool scarf", "sku": "SKU-SCARF", "status": "active", "price": "40",
        }, expect=201)
        data = self.ok("get", "/api/v1/themes/settings/")
        html = self.ok("post", f"/api/v1/themes/preview/?page=product&product={other['id']}", data)["html"]
        self.assertIn("Wool scarf", html)
        self.assertIn('data-tss-section="main"', html)  # Product information, selectable in Edit mode
        self.assertIn('type: "navigate"', html)  # store links open their template in the customizer
        # The home page's product grid links to that product's page
        home = self.ok("post", "/api/v1/themes/preview/?page=index", data)["html"]
        self.assertIn(f"/products/{other['id']}", home)

    def test_unsaved_settings_show_in_preview_only(self):
        data = self.ok("get", "/api/v1/themes/settings/")
        data["templates"]["index"]["sections"]["featured"]["settings"]["show_vendor"] = True
        html = self.ok("post", "/api/v1/themes/preview/?section=featured", data)["html"]
        self.assertIn('class="product-card__vendor"', html)
        self.assertNotIn('class="product-card__vendor"', self.page())

    def test_broken_card_code_is_refused_and_store_keeps_working(self):
        saved = self.files()[CARD]
        r = self.save_card(saved.replace("{%- endif -%}", "", 1), expect=400)
        self.assertIn("line", json.dumps(r).lower())
        self.assertEqual(self.files()[CARD], saved)
        html = self.page()
        self.assertIn("product-card__title", html)
        self.assertNotIn("Liquid error", html)


class BlogTest(StoreClientMixin, APITestCase):
    """Blog posts written in the admin: the blog page, each post's own page, the customizer and the code."""

    def setUp(self):
        self.token, self.store = self.sign_up("blog@example.com", "Blog Store")

    def post(self, expect=201, **fields):
        body = {"title": "My first article", "excerpt": "A short summary.", "body": "<p>Full text here.</p>",
                "author": "Ada Lane", "category": "News", "tags": ["Tips"], "status": "published", **fields}
        return self.ok("post", "/api/v1/themes/posts/", body, expect=expect)

    def page(self, path, expect=200):
        r = self.client.get(f"/s/{self.store['storeSlug']}/{path}")
        self.assertEqual(r.status_code, expect)
        return r.content.decode()

    def preview(self, page, **query):
        data = self.ok("get", "/api/v1/themes/settings/")
        params = "&".join(f"{k}={v}" for k, v in {"page": page, **query}.items())
        return self.ok("post", f"/api/v1/themes/preview/?{params}", data)["html"]

    def test_blog_page_lists_published_posts_as_cards(self):
        first = self.post()
        self.post(title="Second post", status="draft")
        self.assertEqual(first["handle"], "my-first-article")
        self.assertIsNotNone(first["published_at"])
        html = self.page("blog")
        self.assertIn("My first article", html)
        self.assertIn("A short summary.", html)
        self.assertIn(f'/blog/{first["handle"]}', html)
        self.assertIn("post-card__more", html)  # Read more
        self.assertNotIn("Second post", html)  # drafts stay hidden

    def test_article_has_its_own_url_and_reloads_the_same_post(self):
        self.post(title="Older post", cover_image="https://example.com/cover.jpg")
        self.post()
        for _ in range(2):  # opening it again (a refresh) shows the same post
            html = self.page("blog/older-post")
            self.assertIn("<h1 class=\"article__title\">Older post</h1>", html)
            self.assertIn("https://example.com/cover.jpg", html)
            self.assertIn("Ada Lane", html)
            self.assertIn("Full text here.", html)
            self.assertIn("Keep reading", html)  # related posts
            self.assertIn("My first article", html)

    def test_drafts_and_unknown_posts_are_not_found(self):
        self.post(title="Hidden", status="draft")
        self.page("blog/hidden", expect=404)
        self.page("blog/nope", expect=404)
        self.page("blog/unknown/news", expect=404)

    def test_handles_are_unique_per_store(self):
        a = self.post()
        b = self.post()
        self.assertEqual(b["handle"], "my-first-article-2")
        r = self.ok("patch", f"/api/v1/themes/posts/{b['id']}/", {"handle": a["handle"]}, expect=400)
        self.assertIn("handle", r)

    def test_edit_unpublish_and_delete(self):
        post = self.post()
        self.ok("patch", f"/api/v1/themes/posts/{post['id']}/", {"title": "Renamed", "body": "<p>New text</p>"})
        self.assertIn("New text", self.page("blog/my-first-article"))
        self.ok("patch", f"/api/v1/themes/posts/{post['id']}/", {"status": "draft"})
        self.page("blog/my-first-article", expect=404)
        self.ok("delete", f"/api/v1/themes/posts/{post['id']}/", expect=204)
        self.assertEqual(self.ok("get", "/api/v1/themes/posts/"), [])

    def test_body_is_cleaned(self):
        self.post(body='<p onclick="x()">Safe</p><script>alert(1)</script>')
        html = self.page("blog/my-first-article")
        self.assertIn("Safe", html)
        self.assertNotIn("alert(1)", html)
        self.assertNotIn("onclick", html)

    def test_category_and_tag_links_filter_the_blog(self):
        self.post()
        self.post(title="Other", category="Recipes", tags=[])
        html = self.page("blog/category/news")
        self.assertIn("My first article", html)
        self.assertNotIn(">Other<", html)
        self.assertNotIn(">Other<", self.page("blog/tagged/tips"))

    def test_customizer_previews_blog_and_each_article_separately(self):
        samples = self.preview("blog")
        self.assertIn("Five summer drinks", samples)  # samples until a post is published
        self.assertNotIn("/blog/five-summer-drinks", samples)  # ... which link to the blog, never to a missing post
        self.post(title="Older post")
        self.post()
        self.assertIn("My first article", self.preview("blog"))
        self.assertIn('article__title">My first article', self.preview("article"))  # newest
        self.assertIn('article__title">Older post', self.preview("article", article="older-post"))

    def test_article_settings_and_code_reach_the_store(self):
        self.post()
        data = self.ok("get", "/api/v1/themes/settings/")
        data["templates"]["article"]["sections"]["main"]["settings"]["related_count"] = 0
        data["templates"]["article"]["sections"]["main"]["settings"]["show_author"] = False
        self.ok("put", "/api/v1/themes/settings/", data)
        self.post(title="Second")
        html = self.page("blog/my-first-article")
        self.assertNotIn("Keep reading", html)
        self.assertNotIn('href="/s/', html.split("article__byline")[1].split("</header>")[0])  # no author link

        files = {f["path"]: f["content"] for f in self.ok("get", "/api/v1/themes/files/")["files"]}
        path = "sections/main-article.liquid"
        edited = files[path].replace('<h1 class="article__title">', '<p class="custom-kicker">Story</p><h1 class="article__title">')
        self.ok("post", "/api/v1/themes/files/", {"path": path, "content": edited})
        self.assertIn("custom-kicker", self.page("blog/my-first-article"))
        # Saving settings again keeps the custom code
        self.ok("put", "/api/v1/themes/settings/", self.ok("get", "/api/v1/themes/settings/"))
        files = {f["path"]: f["content"] for f in self.ok("get", "/api/v1/themes/files/")["files"]}
        self.assertEqual(files[path], edited)

    def test_other_stores_cannot_see_or_change_posts(self):
        post = self.post()
        self.sign_up("other-blog@example.com", "Other Blog Store")
        self.ok("get", f"/api/v1/themes/posts/{post['id']}/", expect=404)
        self.assertEqual(self.ok("get", "/api/v1/themes/posts/"), [])
