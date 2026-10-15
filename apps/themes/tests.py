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

    def test_wishlist_visibility_is_customizable_and_persists(self):
        from .services import starter_files

        header = self.section()
        setting = header["spec"]["settings"]["show_wishlist"]
        self.assertEqual(setting["type"], "checkbox")
        self.assertEqual(setting["label"], "Show Wishlist")
        self.assertTrue(header["values"]["settings"]["show_wishlist"])
        styles = next(file["content"] for file in self.ok("get", "/api/v1/themes/files/")["files"]
                      if file["path"] == "snippets/styles.liquid")
        self.assertIn(".site-header__wishlist", styles)
        self.assertIn("width: 20px; height: 20px;", styles)
        self.assertIn("stroke-width: 2;", styles)
        self.assertIn("@media (max-width: 640px)", styles)
        self.assertIn('.site-header[data-layout="center"] .site-header__actions', styles)
        self.assertIn(
            'M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 '
            '7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78Z',
            starter_files()["sections/header.liquid"],
        )

        visible = self.home()
        self.assertIn('aria-label="Wishlist"', self.header_html(visible))
        self.assertIn(f'href="/s/{self.store["storeSlug"]}/wishlist/"', self.header_html(visible))
        self.assertIn('data-wishlist-count aria-live="polite">0</span>', self.header_html(visible))

        values = header["values"]
        values["settings"]["show_wishlist"] = False
        self.save_values(values, header["version"])
        refreshed = self.section()
        self.assertFalse(refreshed["values"]["settings"]["show_wishlist"])
        self.assertNotIn('aria-label="Wishlist"', self.header_html(self.home()))

        values = refreshed["values"]
        values["settings"]["show_wishlist"] = True
        self.save_values(values, refreshed["version"])
        self.assertIn('aria-label="Wishlist"', self.header_html(self.home()))
        self.assertIn(f'href="/s/{self.store["storeSlug"]}/wishlist/"', self.header_html(self.home()))

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

    def test_existing_synced_header_gets_wishlist_setting_without_losing_customizations(self):
        import re

        from .services import _add_header_wishlist_setting, starter_files

        content = starter_files()["sections/header.liquid"]
        content = re.sub(
            r'    \{%- if s\.show_wishlist -%}\r?\n.*?    \{%- endif -%}\r?\n',
            "",
            content,
            count=1,
            flags=re.DOTALL,
        )
        content = re.sub(
            r'^\s*\{"type": "checkbox", "id": "show_wishlist".*\r?\n',
            "",
            content,
            count=1,
            flags=re.MULTILINE,
        )
        content = content.replace('"id": "logo_text", "label": "Logo text", "default": ""',
                                  '"id": "logo_text", "label": "Logo text", "default": "Saved brand"')
        crlf_upgraded = _add_header_wishlist_setting(content.replace("\n", "\r\n"))
        self.assertIsNotNone(crlf_upgraded)
        self.assertIn('aria-label="Wishlist"', crlf_upgraded)
        self.assertNotIn("\r\r\n", crlf_upgraded)
        sync.parse(crlf_upgraded, "sections/header.liquid")
        self.theme.files.filter(file_path="sections/header.liquid").update(
            content=content,
            file_size=len(content.encode("utf-8")),
        )
        self.theme.files.filter(file_path="snippets/styles.liquid").update(
            content="  .site-header__nav a:hover { opacity: 1; }\n",
            file_size=len("  .site-header__nav a:hover { opacity: 1; }\n"),
        )
        self.theme.theme_data = {
            **self.theme.theme_data,
            "synced_sections": {"header": 2, "footer": sync.ORIGINALS_REVISION, "index": sync.ORIGINALS_REVISION},
        }
        self.theme.save()

        self.assertEqual(self.upgrade(), 1)
        upgraded = self.theme.files.get(file_path="sections/header.liquid").content
        self.assertIn('"id": "show_wishlist", "label": "Show Wishlist", "default": true', upgraded)
        self.assertIn('aria-label="Wishlist"', upgraded)
        self.assertIn('href="{{ shop.url }}/wishlist/"', upgraded)
        self.assertIn('data-wishlist-count aria-live="polite">{{ wishlist_count }}</span>', upgraded)
        self.assertIn('"default": "Saved brand"', upgraded)
        self.assertEqual(self.upgrade(), 1)
        self.assertEqual(self.theme.files.get(file_path="sections/header.liquid").content, upgraded)
        upgraded_theme = self.theme.__class__.objects.get(pk=self.theme.pk)
        self.assertEqual(sync.section_state(upgraded_theme, "header")["source"], "upgrade")

        header = self.ok("get", "/api/v1/themes/sections/")["header"]
        self.assertTrue(header["values"]["settings"]["show_wishlist"])
        styles = self.theme.files.get(file_path="snippets/styles.liquid").content
        self.assertIn(".site-header__wishlist", styles)
        self.assertIn("@media (max-width: 640px)", styles)
        home = self.client.get(f"/s/{self.store.store_slug}/")
        self.assertEqual(home.status_code, 200)
        self.assertIn('aria-label="Wishlist"', home.content.decode())

    def test_existing_wishlist_icon_and_styles_are_upgraded(self):
        from .services import starter_files

        old_path = (
            'M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 '
            '7.8l1.1 1.1L12 21l7.8-7.5 1.1-1.1a5.5 5.5 0 0 0-.1-7.8Z'
        )
        content = starter_files()["sections/header.liquid"].replace(
            "M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 "
            "7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78Z",
            old_path,
        )
        content = content.replace(
            '<a class="site-header__wishlist" href="{{ shop.url }}/wishlist/" aria-label="Wishlist" title="Wishlist">',
            '<button class="site-header__wishlist" type="button" aria-label="Wishlist" title="Wishlist">',
            1,
        ).replace(
            "        </a>\n      </div>\n    {%- endif -%}",
            "        </button>\n      </div>\n    {%- endif -%}",
            1,
        )
        old_styles = starter_files()["snippets/styles.liquid"].replace(
            "width: 20px; height: 20px;",
            "width: 22px; height: 22px;",
        ).replace("stroke-width: 2;", "stroke-width: 1.8;")
        self.theme.files.filter(file_path="sections/header.liquid").update(
            content=content,
            file_size=len(content.encode("utf-8")),
        )
        self.theme.files.filter(file_path="snippets/styles.liquid").update(
            content=old_styles,
            file_size=len(old_styles.encode("utf-8")),
        )
        self.theme.theme_data = {
            **self.theme.theme_data,
            "synced_sections": {"header": 3, "footer": sync.ORIGINALS_REVISION, "index": sync.ORIGINALS_REVISION},
        }
        self.theme.save()

        self.assertEqual(self.upgrade(), 1)
        upgraded_header = self.theme.files.get(file_path="sections/header.liquid").content
        self.assertIn("M20.84 4.61", upgraded_header)
        self.assertIn('href="{{ shop.url }}/wishlist/"', upgraded_header)
        self.assertNotIn('<button class="site-header__wishlist"', upgraded_header)
        styles = self.theme.files.get(file_path="snippets/styles.liquid").content
        self.assertIn("width: 20px; height: 20px;", styles)
        self.assertIn("stroke-width: 2;", styles)
        self.assertNotIn("width: 22px; height: 22px;", styles)


class StorefrontWishlistTest(StoreClientMixin, APITestCase):
    def setUp(self):
        self.token, self.store = self.sign_up("wishlist@example.com", "Wishlist Store")
        self.product = self.ok("post", "/api/v1/catalog/products/", {
            "title": "Blue mug", "sku": "SKU-MUG", "status": "active", "price": "12.50",
        }, expect=201)
        self.base = f"/s/{self.store['storeSlug']}"

    def test_empty_wishlist_and_header_navigation(self):
        response = self.client.get(f"{self.base}/wishlist/")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Your wishlist is empty.", html)
        self.assertIn(f'href="{self.base}/collections/all">Continue shopping</a>', html)
        self.assertIn(f'href="{self.base}/wishlist/" aria-label="Wishlist"', html)
        self.assertIn('class="wishlist-page__empty"', html)
        from .rendering import SECTION_CSS
        self.assertIn(".wishlist-page__grid{display:grid;grid-template-columns:repeat(2", SECTION_CSS)
        self.assertIn("@media (min-width:750px){.wishlist-page__grid", SECTION_CSS)

    def test_add_displays_product_and_session_retains_it_until_removed(self):
        from apps.catalog.models import ProductImage

        ProductImage.objects.create(
            product_id=self.product["id"],
            url="/media/blue-mug.png",
            position=0,
        )
        add = self.client.post(f"{self.base}/wishlist/items/", {
            "action": "add", "product_id": self.product["id"], "return_to": f"{self.base}/wishlist/",
        })
        self.assertEqual(add.status_code, 302)
        first_load = self.client.get(f"{self.base}/wishlist/")
        self.assertEqual(first_load.status_code, 200)
        html = first_load.content.decode()
        header = html[html.index('<header class="site-header"'):html.index("</header>")]
        self.assertIn("Blue mug", html)
        self.assertIn('<img src="/media/blue-mug.png" alt="Blue mug" loading="lazy">', html)
        self.assertIn("12.50", html)
        self.assertIn("product-card__media--portrait", html)
        self.assertIn(".wishlist-page__grid{display:grid;grid-template-columns:repeat(2", html)
        self.assertIn(".product-card__wishlist-add.is-wishlisted svg", html)
        self.assertIn('.product-card__wishlist-add[aria-pressed="true"] svg', html)
        self.assertIn('.product__wishlist-add[aria-pressed="true"] svg', html)
        self.assertIn('button.closest("form[data-wishlist-form]")', html)  # the script now lives in snippets/wishlist.liquid
        self.assertIn('class="product-card__wishlist-remove"', html)
        self.assertIn('class="product-card__wishlist-add is-wishlisted"', html)
        self.assertIn('aria-pressed="true"', html)
        self.assertIn('data-wishlist-count aria-live="polite">1</span>', header)
        self.assertNotIn("Your wishlist is empty.", html)

        collection_html = self.client.get(f"{self.base}/collections/all").content.decode()
        self.assertIn('class="product-card__wishlist-add is-wishlisted"', collection_html)
        self.assertIn('aria-pressed="true"', collection_html)
        product_html = self.client.get(f"{self.base}/products/{self.product['id']}").content.decode()
        self.assertIn('class="product__wishlist-add is-wishlisted"', product_html)
        self.assertIn('aria-pressed="true"', product_html)

        refreshed = self.client.get(f"{self.base}/wishlist/")
        self.assertIn("Blue mug", refreshed.content.decode())
        self.assertIn('data-wishlist-count aria-live="polite">1</span>', refreshed.content.decode())
        duplicate = self.client.post(f"{self.base}/wishlist/items/", {
            "action": "add", "product_id": self.product["id"], "return_to": f"{self.base}/wishlist/",
        })
        self.assertEqual(duplicate.status_code, 302)
        self.assertEqual(len(self.client.session[f"wishlist:{self.store['storeId']}"]), 1)

        remove = self.client.post(f"{self.base}/wishlist/items/", {
            "action": "remove", "product_id": self.product["id"], "return_to": f"{self.base}/wishlist/",
        })
        self.assertEqual(remove.status_code, 302)
        empty = self.client.get(f"{self.base}/wishlist/").content.decode()
        self.assertIn("Your wishlist is empty.", empty)
        self.assertNotIn("Blue mug", empty)
        self.assertIn('data-wishlist-count aria-live="polite">0</span>', empty)
        collection_after_remove = self.client.get(f"{self.base}/collections/all").content.decode()
        self.assertIn('class="product-card__wishlist-add"', collection_after_remove)
        self.assertNotIn('class="product-card__wishlist-add is-wishlisted"', collection_after_remove)

    def test_script_requests_json_and_gets_the_new_count(self):
        """The heart script posts with Accept: application/json and updates the count without a reload."""
        other = self.ok("post", "/api/v1/catalog/products/", {
            "title": "Red mug", "sku": "SKU-RED", "status": "active", "price": "9.00",
        }, expect=201)
        url = f"{self.base}/wishlist/items/"
        headers = {"HTTP_ACCEPT": "application/json"}
        first = self.client.post(url, {"action": "add", "product_id": self.product["id"]}, **headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), {"saved": True, "count": 1, "product_id": self.product["id"]})
        second = self.client.post(url, {"action": "add", "product_id": other["id"]}, **headers)
        self.assertEqual(second.json()["count"], 2)
        removed = self.client.post(url, {"action": "remove", "product_id": self.product["id"]}, **headers)
        self.assertEqual(removed.json(), {"saved": False, "count": 1, "product_id": self.product["id"]})
        # no Accept header: the usual redirect, so it still works without JavaScript
        self.assertEqual(self.client.post(url, {"action": "remove", "product_id": other["id"]}).status_code, 302)

    def test_add_from_product_page_and_invalid_redirect(self):
        product_page = self.client.get(f"{self.base}/products/{self.product['id']}")
        self.assertEqual(product_page.status_code, 200)
        self.assertIn('class="product__wishlist-form"', product_page.content.decode())
        collection_page = self.client.get(f"{self.base}/collections/all")
        self.assertIn('class="product-card__wishlist-form"', collection_page.content.decode())
        response = self.client.post(f"{self.base}/wishlist/items/", {
            "action": "add", "product_id": self.product["id"], "return_to": "https://example.com/",
        })
        self.assertEqual(response["Location"], f"{self.base}/wishlist/")
        self.assertIn("Blue mug", self.client.get(f"{self.base}/wishlist/").content.decode())


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


GRID = "sections/product-grid.liquid"
INDEX_JSON = "templates/index.json"


class ProductGridSyncTest(StoreClientMixin, APITestCase):
    """
    The Home page's Product grid ("featured") in both editors: its values are the "featured" entry of
    templates/index.json in Edit code and its panel in the customizer (one saved copy); its markup is
    sections/product-grid.liquid, which neither editor's value changes ever rewrite.
    """

    def setUp(self):
        self.token, self.store = self.sign_up("grid@example.com", "Grid Store")
        self.ok("post", "/api/v1/catalog/products/", {
            "title": "Clay mug", "sku": "SKU-MUG", "status": "active", "price": "12", "vendor": "Potter",
        }, expect=201)

    def files(self):
        return {f["path"]: f["content"] for f in self.ok("get", "/api/v1/themes/files/")["files"]}

    def save_file(self, path, content, expect=200):
        r = self.client.post("/api/v1/themes/files/", {"path": path, "content": content}, format="json")
        self.assertEqual(r.status_code, expect, r.content[:500])
        return r.json()

    def code_index(self):
        return json.loads(self.files()[INDEX_JSON])

    def save_code_index(self, change):
        index = self.code_index()
        change(index["sections"]["featured"])
        self.save_file(INDEX_JSON, json.dumps(index, indent=2))

    def customizer_grid(self):
        return self.ok("get", "/api/v1/themes/settings/")["templates"]["index"]["sections"]["featured"]

    def save_customizer_grid(self, change):
        data = self.ok("get", "/api/v1/themes/settings/")
        section = data["templates"]["index"]["sections"]["featured"]
        change(section)
        self.ok("patch", "/api/v1/themes/settings/", {"templates": {"index": {
            "order": data["templates"]["index"]["order"], "sections": {"featured": section}}}})

    def store_html(self):
        r = self.client.get(f"/s/{self.store['storeSlug']}/")
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def preview_html(self):
        data = self.ok("get", "/api/v1/themes/settings/")
        return self.ok("post", "/api/v1/themes/preview/?section=featured", data)["html"]

    def heading_blocks(self, *texts):
        return [{"type": "heading", "settings": {"text": t}} for t in texts]

    def test_1_code_setting_reaches_customizer_preview_and_store(self):
        self.save_code_index(lambda s: s["settings"].update(heading="From code", show_vendor=True))
        self.assertEqual(self.customizer_grid()["settings"]["heading"], "From code")
        self.assertTrue(self.customizer_grid()["settings"]["show_vendor"])
        for html in (self.preview_html(), self.store_html()):
            self.assertIn("From code", html)
            self.assertIn('class="product-card__vendor"', html)

    def test_2_customizer_setting_reaches_code(self):
        self.save_customizer_grid(lambda s: s["settings"].update(heading="From sidebar", columns=3))
        featured = self.code_index()["sections"]["featured"]
        self.assertEqual(featured["settings"]["heading"], "From sidebar")
        self.assertEqual(featured["settings"]["columns"], 3)
        self.assertIn("From sidebar", self.store_html())

    def test_3_blocks_add_reorder_hide_remove_in_both_editors(self):
        # Added in the code
        self.save_code_index(lambda s: s.update(blocks=self.heading_blocks("Block A", "Block B")))
        self.assertEqual([b["settings"]["text"] for b in self.customizer_grid()["blocks"]], ["Block A", "Block B"])
        html = self.store_html()
        self.assertLess(html.index("Block A"), html.index("Block B"))
        # Reordered in the customizer
        self.save_customizer_grid(lambda s: s["blocks"].reverse())
        self.assertEqual([b["settings"]["text"] for b in self.code_index()["sections"]["featured"]["blocks"]],
                         ["Block B", "Block A"])
        html = self.store_html()
        self.assertLess(html.index("Block B"), html.index("Block A"))
        # Hidden in the customizer, shown again in the code
        self.save_customizer_grid(lambda s: s["blocks"][0].update(hidden=True))
        self.assertTrue(self.code_index()["sections"]["featured"]["blocks"][0]["hidden"])
        self.assertNotIn("Block B", self.store_html())
        self.save_code_index(lambda s: s["blocks"][0].update(hidden=False))
        self.assertFalse(self.customizer_grid()["blocks"][0].get("hidden"))
        self.assertIn("Block B", self.preview_html())
        # Removed in the code
        self.save_code_index(lambda s: s["blocks"].pop(0))
        self.assertEqual([b["settings"]["text"] for b in self.customizer_grid()["blocks"]], ["Block A"])
        self.assertNotIn("Block B", self.store_html())

    def test_4_custom_markup_survives_both_editors(self):
        edited = self.files()[GRID].replace(
            '<section id="section-{{ section.id }}"', '<p class="grid-note">Hand made</p><section id="section-{{ section.id }}"', 1)
        self.assertNotEqual(edited, self.files()[GRID])
        self.save_file(GRID, edited)
        self.save_customizer_grid(lambda s: s["settings"].update(heading="Sidebar"))
        self.save_code_index(lambda s: s["settings"].update(heading="Code"))
        self.assertEqual(self.files()[GRID], edited)  # neither value change rewrote the markup
        self.assertIn('class="grid-note"', self.preview_html())
        self.assertIn('class="grid-note"', self.store_html())

    def test_5_invalid_grid_code_is_refused_and_store_keeps_working(self):
        saved = self.files()[GRID]
        r = self.save_file(GRID, saved.replace("{%- endif -%}", "", 1), expect=400)
        self.assertIn("Line", r["content"][0] if isinstance(r["content"], list) else r["content"])
        self.assertEqual(self.files()[GRID], saved)
        self.assertNotIn("Liquid error", self.store_html())

    def test_6_layout_cards_and_show_price(self):
        html = self.store_html()
        self.assertIn('class="product-card', html)
        self.assertIn("Clay mug", html)
        prices = html.count('<span class="price">')
        self.assertGreater(prices, 0)
        self.save_code_index(lambda s: s["settings"].update(show_price=False))
        self.assertLess(self.store_html().count('<span class="price">'), prices)  # the carousel keeps its own
        self.save_customizer_grid(lambda s: s["settings"].update(show_price=True))
        self.assertEqual(self.store_html().count('<span class="price">'), prices)
        self.assertTrue(self.code_index()["sections"]["featured"]["settings"]["show_price"])


STYLES = "snippets/styles.liquid"
SETTINGS_JSON = "config/settings_data.json"


class StylesSyncTest(StoreClientMixin, APITestCase):
    """snippets/styles.liquid (Edit code) and the Theme settings colours and type (customizer / settings_data.json)."""

    def setUp(self):
        self.token, self.store = self.sign_up("styles@example.com", "Styles Store")
        self.ok("post", "/api/v1/catalog/products/", {
            "title": "Linen apron", "sku": "SKU-APRON", "status": "active", "price": "30",
        }, expect=201)

    def files(self):
        return {f["path"]: f["content"] for f in self.ok("get", "/api/v1/themes/files/")["files"]}

    def save_file(self, path, content):
        r = self.client.post("/api/v1/themes/files/", {"path": path, "content": content}, format="json")
        self.assertEqual(r.status_code, 200, r.content[:500])

    def store_html(self):
        r = self.client.get(f"/s/{self.store['storeSlug']}/")
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def preview_html(self, data=None):
        data = data or self.ok("get", "/api/v1/themes/settings/")
        return self.ok("post", "/api/v1/themes/preview/", data)["html"]

    def test_css_edit_reaches_preview_and_store_and_persists(self):
        original = self.files()[STYLES]
        old_rule, new_rule = ".product-card__title { font-weight: 600; }", ".product-card__title { font-weight: 700; }"
        self.assertIn(old_rule, original)
        self.save_file(STYLES, original.replace(old_rule, new_rule))  # the temporary change
        self.assertIn(new_rule, self.files()[STYLES])  # what reopening the code editor shows
        self.assertIn(new_rule, self.preview_html())
        for _ in range(2):  # the storefront, and again after a refresh
            html = self.store_html()
            self.assertIn(new_rule, html)
            self.assertIn('class="product-card__title"', html)
        self.save_file(STYLES, original)  # restored
        self.assertIn(old_rule, self.store_html())
        self.assertNotIn(new_rule, self.store_html())

    def test_theme_colours_fill_the_css_variables(self):
        html = self.store_html()
        for line in ("--accent: #161C2C;", "--bg: #FFFFFF;", "--text: #161C2C;"):  # the defaults
            self.assertIn(line, html)

        data = self.ok("get", "/api/v1/themes/settings/")
        data["settings"].update(accent_color="#AA2200", background_color="#FAF7F2", text_color="#222222")
        self.assertIn("#AA2200", self.preview_html(data).upper())  # unsaved: the preview only
        self.assertNotIn("#AA2200", self.store_html().upper())

        self.ok("patch", "/api/v1/themes/settings/", {"settings": data["settings"]})  # saved from the customizer
        html = self.store_html().upper()
        for line in ("--ACCENT: #AA2200;", "--BG: #FAF7F2;", "--TEXT: #222222;"):
            self.assertIn(line, html)
        code_settings = json.loads(self.files()[SETTINGS_JSON])  # what Edit code shows
        self.assertEqual(code_settings["accent_color"].upper(), "#AA2200")
        self.assertIn("{{ settings.accent_color }}", self.files()[STYLES])  # the CSS file itself is not rewritten

    def test_settings_json_from_code_reaches_customizer_and_store(self):
        code_settings = json.loads(self.files()[SETTINGS_JSON])
        code_settings.update(text_color="#123456", base_font_size=18)
        self.save_file(SETTINGS_JSON, json.dumps(code_settings, indent=2))
        saved = self.ok("get", "/api/v1/themes/settings/")["settings"]
        self.assertEqual(saved["text_color"].upper(), "#123456")
        self.assertEqual(saved["base_font_size"], 18)
        html = self.store_html()
        self.assertIn("--text: #123456;", html)
        self.assertIn("--tss-base-size:18px", html.replace(" ", ""))


class CustomizerTokenTest(StoreClientMixin, APITestCase):
    """Customizer tokens (POST /themes/customizer/) in the CSS of the preview and the storefront."""

    TOKENS = {"primary": "#101010", "accent": "#FF5500", "background": "#F0F0F0",
              "heading_font": "serif", "body_font": "Verdana, sans-serif", "button_radius": "8px"}

    def setUp(self):
        self.token, self.store = self.sign_up("tokens@example.com", "Token Store")

    def store_html(self):
        r = self.client.get(f"/s/{self.store['storeSlug']}/")
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def preview_html(self):
        return self.ok("post", "/api/v1/themes/preview/", self.ok("get", "/api/v1/themes/settings/"))["html"]

    def token_css(self, html):
        """The theme's :root <style> that the tokens go into (it holds --tss-base-size)."""
        start = html.index("<style>:root{--tss-heading-font")
        return html[start:html.index("</style>", start)]

    def test_no_tokens_keeps_the_theme_settings(self):
        css = self.token_css(self.store_html())
        self.assertNotIn("--accent:", css)
        self.assertNotIn("--text:", css)
        self.assertIn("--b-r:999px", css)  # the theme's pill buttons

    def test_saved_tokens_reach_store_and_preview_and_persist(self):
        self.assertEqual(self.ok("post", "/api/v1/themes/customizer/", self.TOKENS), self.TOKENS)
        self.assertEqual(self.ok("get", "/api/v1/themes/customizer/"), self.TOKENS)  # saved
        expected = ["--text:#101010", "--accent:#FF5500", "--bg:#F0F0F0",
                    '--tss-heading-font:Georgia,"Times New Roman",serif', "--tss-body-font:Verdana, sans-serif",
                    "--b-r:8px"]
        for html in (self.store_html(), self.store_html(), self.preview_html()):  # store, refresh, preview
            css = self.token_css(html)
            for line in expected:
                self.assertIn(line, css)
            # Comes after styles.liquid's :root, so the token wins over settings.accent_color
            self.assertLess(html.index("--accent: #161C2C;"), html.index("--accent:#FF5500"))
        self.assertEqual(self.store_html().count("--accent:#FF5500"), 1)  # loaded once

    def test_theme_settings_colours_still_work_next_to_tokens(self):
        self.ok("post", "/api/v1/themes/customizer/", {"accent": "#FF5500"})
        self.ok("patch", "/api/v1/themes/settings/", {"settings": {"text_color": "#334455", "background_color": "#FFFFEE"}})
        html = self.store_html()
        self.assertIn("--text: #334455;", html)  # no primary token: the setting decides
        self.assertIn("--bg: #FFFFEE;", html)
        css = self.token_css(html)
        self.assertIn("--accent:#FF5500", css)
        self.assertNotIn("--text:", css)
        self.assertNotIn("--bg:", css)

    def test_unsafe_token_is_refused(self):
        r = self.client.post("/api/v1/themes/customizer/", {"accent": "red;}</style><script>"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("<script>", self.token_css(self.store_html()))


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


class StorePagesTest(StoreClientMixin, APITestCase):
    """Pages written in the admin (About us, ...): /pages/<handle>, the customizer and the code editor."""

    def setUp(self):
        self.token, self.store = self.sign_up("pages@example.com", "Pages Store")

    def create(self, expect=201, **fields):
        body = {"title": "About us", "body": "<p>Our story.</p>", "status": "visible", **fields}
        return self.ok("post", "/api/v1/themes/pages/", body, expect=expect)

    def page(self, path, expect=200):
        r = self.client.get(f"/s/{self.store['storeSlug']}/{path}")
        self.assertEqual(r.status_code, expect)
        return r.content.decode()

    def preview(self, **query):
        data = self.ok("get", "/api/v1/themes/settings/")
        params = "&".join(f"{k}={v}" for k, v in {"page": "page", **query}.items())
        return self.ok("post", f"/api/v1/themes/preview/?{params}", data)["html"]

    def test_visible_page_has_its_own_url_and_hidden_one_is_not_found(self):
        about = self.create()
        self.assertEqual(about["handle"], "about-us")
        self.assertEqual(self.create()["handle"], "about-us-2")
        html = self.page("pages/about-us")
        self.assertIn('<h1 class="page__title">About us</h1>', html)
        self.assertIn("Our story.", html)
        self.ok("patch", f"/api/v1/themes/pages/{about['id']}/", {"status": "hidden"})
        self.page("pages/about-us", expect=404)
        self.page("pages/nope", expect=404)

    def test_edit_and_delete(self):
        about = self.create()
        self.ok("patch", f"/api/v1/themes/pages/{about['id']}/", {"body": "<p>New text</p>", "seo_title": "Hello"})
        html = self.page("pages/about-us")
        self.assertIn("New text", html)
        self.assertIn("<title>Hello", html)
        self.ok("delete", f"/api/v1/themes/pages/{about['id']}/", expect=204)
        self.page("pages/about-us", expect=404)

    def test_customizer_previews_a_sample_page_until_there_is_one(self):
        self.assertIn("Our story", self.preview())
        self.create(title="Shipping", body="<p>Ships in 2 days.</p>")
        self.assertIn("Ships in 2 days.", self.preview())

    def test_template_and_section_reach_the_code_editor_and_the_store(self):
        files = {f["path"]: f["content"] for f in self.ok("get", "/api/v1/themes/files/")["files"]}
        self.assertIn("templates/page.liquid", files)
        self.assertIn("sections/main-page.liquid", files)
        self.assertIn("templates/page.json", files)
        self.create()
        path = "sections/main-page.liquid"
        edited = files[path].replace('<h1 class="page__title">', '<p class="custom-kicker">Hi</p><h1 class="page__title">')
        self.ok("post", "/api/v1/themes/files/", {"path": path, "content": edited})
        self.assertIn("custom-kicker", self.page("pages/about-us"))

    def test_other_stores_cannot_see_or_change_pages(self):
        about = self.create()
        self.sign_up("other-pages@example.com", "Other Pages Store")
        self.ok("get", f"/api/v1/themes/pages/{about['id']}/", expect=404)
        self.assertEqual(self.ok("get", "/api/v1/themes/pages/"), [])


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
