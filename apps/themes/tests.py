"""
The footer's two-way sync: sections/footer.liquid <-> the customizer's Footer.

    python manage.py test apps.themes --settings=config.test_settings
"""
import json
import time

from rest_framework.test import APITestCase

from apps.core.tests.test_flow import StoreClientMixin

from . import section_sync as sync

PATH = "sections/footer.liquid"


class FooterSyncTest(StoreClientMixin, APITestCase):
    # "change made -> preview updated", measured through the API (all tests together)
    timings = {"sidebar -> code + preview": [], "code save -> blocks + preview": []}

    def setUp(self):
        self.token, self.store = self.sign_up("footer@example.com", "Footer Store")

    # ---------------------------------------------------------------- helpers

    def code(self):
        files = self.ok("get", "/api/v1/themes/files/")["files"]
        return next(f["content"] for f in files if f["path"] == PATH)

    def footer(self):
        return self.ok("get", "/api/v1/themes/sections/")["footer"]

    def save_values(self, values, version=None):
        started = time.perf_counter()
        body = {"sections": {"footer": values}, "client": "test"}
        if version is not None:
            body["section_versions"] = {"footer": version}
        saved = self.ok("patch", "/api/v1/themes/settings/", body)["sections"]["footer"]
        preview = self.preview(values)
        self.timings["sidebar -> code + preview"].append((time.perf_counter() - started) * 1000)
        return saved, preview

    def save_code(self, content, expect=200, version=None):
        started = time.perf_counter()
        body = {"path": PATH, "content": content}
        if version is not None:
            body["base_version"] = version
        r = self.client.post("/api/v1/themes/files/", body, format="json")
        self.assertEqual(r.status_code, expect, r.content[:500])
        if expect == 200:
            self.preview(r.json()["sync"]["values"])
            self.timings["code save -> blocks + preview"].append((time.perf_counter() - started) * 1000)
        return r.json()

    def preview(self, values):
        """The customizer's footer-only preview request."""
        data = self.ok("get", "/api/v1/themes/settings/")
        data["sections"] = {"footer": values}
        return self.ok("post", "/api/v1/themes/preview/?section=footer", data)["html"]

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
                print(f"\n  [footer sync] {kind}: average {sum(values) / len(values):.0f} ms, "
                      f"slowest {max(values):.0f} ms ({len(values)} runs)")

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
        self.assertNotIn('data-owner="a"', next(f["content"] for f in files if f["path"] == PATH))

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
