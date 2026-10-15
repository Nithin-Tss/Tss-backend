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


# The starter header's loop over the wishlist and cart icons, in block order
HEADER_ICONS_LOOP = """    {%- if wishlist_blocks.size > 0 or cart_blocks.size > 0 -%}
      <div class="site-header__actions">
        {%- for block in section.blocks -%}
          {%- if block.type == 'wishlist' -%}
            {% render 'wishlist', mode: 'icon', count: wishlist_count, label: block.settings.label, show_count: block.settings.show_count, attrs: block.attrs %}
          {%- elsif block.type == 'cart' -%}
            {% render 'cart', mode: 'icon', count: cart_count, session: cart_session, settings: block.settings, attrs: block.attrs %}
          {%- endif -%}
        {%- endfor -%}
      </div>
    {%- endif -%}
"""


def wishlist_era_header():
    """The starter header from before the cart icon: the heart as a block, and no cart (what revision 20 stores have)."""
    from . import section_sync as sync
    from .services import starter_files

    header = starter_files()["sections/header.liquid"]
    for new, old in (
        ("""  The "Cart icon" block shows the cart with the number of items. It opens the side drawer or the cart
  page: everything the cart does is in snippets/cart.liquid.
""", ""),
        ("{%- assign cart_blocks = section.blocks | where: 'type', 'cart' -%}\n", ""),
        (HEADER_ICONS_LOOP, sync.WISHLIST_BLOCK_MARKUP.split("\n", 1)[1]),
        ("    },\n    " + sync.CART_BLOCK_SCHEMA + "\n  ],", "    }\n  ],"),
        (', {"type": "cart", "settings": {}}]}', "]}"),
    ):
        assert new in header, new[:60]
        header = header.replace(new, old, 1)
    return header


def legacy_header():
    """The starter header from before the heart became a block: a "Show Wishlist" checkbox and inline markup."""
    header = wishlist_era_header()
    heart = "M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78Z"
    for new, old in (
        ("""  The "Wishlist icon" block shows the heart with the saved count, on the right. It only links to
  the wishlist page: everything the wishlist does is in snippets/wishlist.liquid.
""", ""),
        ("""{%- assign link_blocks = section.blocks | where: 'type', 'link' -%}
{%- assign wishlist_blocks = section.blocks | where: 'type', 'wishlist' -%}
""", ""),
        ("""{%- if link_blocks.size > 0 -%}
          {%- for block in link_blocks -%}""", """{%- if section.blocks.size > 0 -%}
          {%- for block in section.blocks -%}"""),
        ("""    {%- if wishlist_blocks.size > 0 -%}
      <div class="site-header__actions">
        {%- for block in wishlist_blocks -%}
          {% render 'wishlist', mode: 'icon', count: wishlist_count, label: block.settings.label, show_count: block.settings.show_count, attrs: block.attrs %}
        {%- endfor -%}
      </div>
    {%- endif -%}""", f"""    {{%- if s.show_wishlist -%}}
      <div class="site-header__actions">
        <a class="site-header__wishlist" href="{{{{ shop.url }}}}/wishlist/" aria-label="Wishlist" title="Wishlist">
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="{heart}" />
          </svg>
          <span class="site-header__wishlist-count" data-wishlist-count aria-live="polite">{{{{ wishlist_count }}}}</span>
        </a>
      </div>
    {{%- endif -%}}"""),
        ("""    {"type": "checkbox", "id": "show_nav", "label": "Show the navigation menu", "default": true},
""", """    {"type": "checkbox", "id": "show_nav", "label": "Show the navigation menu", "default": true},
    {"type": "checkbox", "id": "show_wishlist", "label": "Show Wishlist", "default": true},
"""),
        ("""    },
    {
      "type": "wishlist",
      "name": "Wishlist icon",
      "settings": [
        {"type": "text", "id": "label", "label": "Label (for screen readers)", "default": "Wishlist"},
        {"type": "checkbox", "id": "show_count", "label": "Show the number of saved products", "default": true}
      ]
    }
  ],
  "default": {"blocks": [{"type": "wishlist", "settings": {"label": "Wishlist", "show_count": true}}]}""", """    }
  ],
  "default": {"blocks": []}"""),
    ):
        assert new in header, new[:60]
        header = header.replace(new, old, 1)
    return header


def edited_header():
    """
    A header an owner edited: menu links, an account icon, a Profile picture block and their own note,
    and no Wishlist icon block (the shape of the headers that come from older starters or from Edit code).
    """
    from . import section_sync as sync
    from .services import starter_files

    header = wishlist_era_header()
    block_markup = sync.WISHLIST_BLOCK_MARKUP.split("\n", 1)[1]  # without the assign line
    for old, new in (
        ("{%- assign wishlist_blocks = section.blocks | where: 'type', 'wishlist' -%}\n", ""),
        (block_markup, """    {%- if s.show_account -%}
      <a class="site-header__account" href="{{ shop.url }}/account" aria-label="Account">Account</a>
    {%- endif -%}
    {%- for block in section.blocks -%}
      {%- if block.type == 'profile' -%}
        <a class="site-header__profile" href="{{ block.settings.url | default: '/account' }}"{{ block.attrs }}><img src="{{ block.settings.image }}" alt="" width="{{ block.settings.size }}"></a>
      {%- endif -%}
    {%- endfor -%}
    <!-- owner note: keep this line -->
"""),
        ('    {"type": "checkbox", "id": "show_nav", "label": "Show the navigation menu", "default": true},\n',
         '    {"type": "checkbox", "id": "show_nav", "label": "Show the navigation menu", "default": true},\n'
         '    {"type": "checkbox", "id": "show_account", "label": "Show account icon", "default": true},\n'),
        ("    " + sync.WISHLIST_BLOCK_SCHEMA, """    {
      "type": "profile",
      "name": "Profile picture",
      "settings": [
        {"type": "image", "id": "image", "label": "Profile picture", "default": ""},
        {"type": "range", "id": "size", "label": "Picture size", "min": 24, "max": 64, "step": 2, "unit": "px", "default": 36},
        {"type": "url", "id": "url", "label": "Link", "default": "/account"}
      ]
    }"""),
        ('"default": {"blocks": [{"type": "wishlist", "settings": {"label": "Wishlist", "show_count": true}}]}', '"default": {"blocks": []}'),
    ):
        assert old in header, old[:70]
        header = header.replace(old, new, 1)
    return header


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

    def test_wishlist_icon_is_a_block_the_owner_can_add_and_remove(self):
        """The header's heart is the "Wishlist icon" block: add or remove it in the customizer, and the code follows."""
        from .services import starter_files

        header = self.section()
        block = header["spec"]["blocks"]["wishlist"]
        self.assertEqual(block["name"], "Wishlist icon")
        self.assertEqual(block["settings"]["show_count"]["type"], "checkbox")
        self.assertNotIn("show_wishlist", header["spec"]["settings"])  # no more checkbox: it is a block now
        self.assertEqual([b["type"] for b in header["values"]["blocks"]], ["wishlist", "cart"])
        styles = next(file["content"] for file in self.ok("get", "/api/v1/themes/files/")["files"]
                      if file["path"] == "snippets/styles.liquid")
        self.assertIn(".site-header__wishlist", styles)
        self.assertIn("@media (max-width: 640px)", styles)
        self.assertIn('.site-header[data-layout="center"] .site-header__actions', styles)
        # No wishlist logic in the header: it only renders the snippet
        self.assertIn("{% render 'wishlist', mode: 'icon'", starter_files()["sections/header.liquid"])
        self.assertNotIn("M20.84 4.61", starter_files()["sections/header.liquid"])

        visible = self.home()
        self.assertIn('aria-label="Wishlist"', self.header_html(visible))
        self.assertIn(f'href="/s/{self.store["storeSlug"]}/wishlist/"', self.header_html(visible))
        self.assertIn('data-wishlist-count aria-live="polite">0</span>', self.header_html(visible))

        # Remove the block in the customizer: the code and the store update
        values = header["values"]
        values["blocks"] = []
        _, preview = self.save_values(values, header["version"])
        self.assertNotIn("site-header__wishlist", preview)
        self.assertEqual(self.schema_of(self.code())["default"]["blocks"], [])
        self.assertNotIn('aria-label="Wishlist"', self.header_html(self.home()))
        # ... and with no menu links the automatic menu still shows
        self.assertIn(">Home</a>", self.header_html(self.home()))

        # Add it again, with its own settings
        refreshed = self.section()
        values = refreshed["values"]
        values["blocks"] = self.links(("Shop", "/collections/all")) + [
            {"type": "wishlist", "settings": {"label": "My favourites", "show_count": False}}]
        _, preview = self.save_values(values, refreshed["version"])
        html = self.header_html(self.home())
        self.assertIn('aria-label="My favourites"', html)
        self.assertNotIn('data-wishlist-count aria-live', html)  # the count is switched off
        self.assertIn(">Shop</a>", html)  # a menu link, not mistaken for the icon
        self.assertEqual([b["type"] for b in self.schema_of(self.code())["default"]["blocks"]], ["link", "wishlist"])

    def test_wishlist_block_schema_edited_in_code_updates_the_sidebar(self):
        """Edit the wishlist block's schema in Edit code: the customizer's block options follow, values stay."""
        values = self.section()["values"]
        values["blocks"] = [{"type": "wishlist", "settings": {"label": "Saved", "show_count": True}}]
        self.save_values(values)

        def add_setting(schema):
            block = next(b for b in schema["blocks"] if b["type"] == "wishlist")
            block["name"] = "Heart"
            block["settings"].append({"type": "text", "id": "tooltip", "label": "Tooltip", "default": "Your saved items"})

        saved = self.save_code(self.with_schema(self.code(), add_setting))
        spec = saved["sync"]["spec"]["blocks"]["wishlist"]
        self.assertEqual(spec["name"], "Heart")
        self.assertIn("tooltip", spec["settings"])
        block = self.section()["values"]["blocks"][0]
        self.assertEqual(block["settings"]["label"], "Saved")  # the owner's value is kept
        self.assertEqual(block["settings"]["tooltip"], "Your saved items")  # the new option has its default

    def test_invalid_wishlist_block_schema_is_refused_with_its_line(self):
        before = self.code()
        bad = before.replace('"id": "show_count", "label"', '"id": "show_count" "label"', 1)
        error = self.save_code(bad, expect=400)
        self.assertIn("schema is not valid JSON", error["content"][0] if isinstance(error["content"], list) else error["content"])
        line = int(error["line"][0] if isinstance(error["line"], list) else error["line"])
        self.assertIn("show_count", bad.splitlines()[line - 1])  # the error points at the broken wishlist setting
        self.assertEqual(self.code(), before)  # the previous version keeps working
        self.assertIn('aria-label="Wishlist"', self.header_html(self.home()))

    def wishlist_block(self, **settings):
        return {"type": "wishlist", "settings": {"label": "Wishlist", "show_count": True, **settings}}

    def hearts(self, html):
        return html.count('class="site-header__wishlist"')

    def default_block_types(self):
        return [b["type"] for b in self.schema_of(self.code())["default"]["blocks"]]

    def test_wishlist_icon_is_in_the_add_block_list(self):
        spec = self.section()["spec"]["blocks"]
        self.assertEqual(spec["wishlist"]["name"], "Wishlist icon")
        self.assertEqual(set(spec["wishlist"]["settings"]), {"label", "show_count"})
        self.assertEqual(set(spec), {"link", "wishlist", "cart"})  # what "Add block" offers

    def test_adding_hiding_reordering_and_removing_the_icon_in_the_customizer(self):
        header = self.section()
        values = header["values"]
        # remove it (a new store starts with one), then add it after a menu link
        values["blocks"] = []
        saved, preview = self.save_values(values, header["version"])
        self.assertEqual((self.hearts(preview), self.default_block_types()), (0, []))
        values["blocks"] = self.links(("Shop", "/collections/all")) + [self.wishlist_block()]
        _, preview = self.save_values(values)
        self.assertEqual(self.hearts(preview), 1)
        self.assertEqual(self.default_block_types(), ["link", "wishlist"])  # written into the code, in order
        self.assertEqual(self.hearts(self.header_html(self.home())), 1)
        self.assertIn("{% render 'wishlist', mode: 'icon'", self.code())  # the code only calls the snippet

        # reorder: the order is the code's order (the heart is always shown on the right, links in the menu)
        values["blocks"] = [self.wishlist_block()] + self.links(("Shop", "/collections/all"))
        _, preview = self.save_values(values)
        self.assertEqual(self.default_block_types(), ["wishlist", "link"])
        self.assertEqual(self.hearts(preview), 1)
        self.assertIn(">Shop</a>", preview)

        # hide it: kept in the code, not shown
        values["blocks"][0]["hidden"] = True
        _, preview = self.save_values(values)
        self.assertEqual(self.hearts(preview), 0)
        self.assertEqual(self.hearts(self.header_html(self.home())), 0)
        self.assertTrue(self.schema_of(self.code())["default"]["blocks"][0].get("hidden"))
        # show it again
        values["blocks"][0]["hidden"] = False
        _, preview = self.save_values(values)
        self.assertEqual(self.hearts(preview), 1)

        # remove it: gone from the code and the store
        values["blocks"] = self.links(("Shop", "/collections/all"))
        _, preview = self.save_values(values)
        self.assertEqual((self.hearts(preview), self.default_block_types()), (0, ["link"]))
        self.assertEqual(self.hearts(self.header_html(self.home())), 0)

    def test_removing_and_adding_the_block_type_in_code_updates_add_block_the_sidebar_and_the_store(self):
        from . import section_sync as sync

        header = self.section()
        values = header["values"]
        values["blocks"] = [self.wishlist_block()]
        self.save_values(values, header["version"])
        self.assertEqual(self.hearts(self.header_html(self.home())), 1)

        # remove the block type from the code (the schema's "blocks" list and its default blocks)
        without = self.with_schema(self.code(), lambda s: (
            s.update(blocks=[b for b in s["blocks"] if b["type"] != "wishlist"]),
            s["default"].update(blocks=[b for b in s["default"]["blocks"] if b["type"] != "wishlist"]),
        ))
        saved = self.save_code(without)
        self.assertNotIn("wishlist", saved["sync"]["spec"]["blocks"])  # gone from "Add block"
        self.assertNotIn("wishlist", [b["type"] for b in self.section()["values"]["blocks"]])  # and from the sidebar
        self.assertEqual(self.hearts(self.header_html(self.home())), 0)

        # add it back in the code: it is offered again, and the owner can add it in the sidebar
        restored = sync.add_wishlist_block(self.code())
        self.assertIsNotNone(restored)
        saved = self.save_code(restored)
        self.assertEqual(saved["sync"]["spec"]["blocks"]["wishlist"]["name"], "Wishlist icon")
        self.assertEqual(self.hearts(self.header_html(self.home())), 0)  # offered, not added
        current = self.section()
        values = current["values"]
        values["blocks"] = [self.wishlist_block(label="Saved items")]
        _, preview = self.save_values(values, current["version"])
        self.assertIn('aria-label="Saved items"', preview)
        self.assertIn('aria-label="Saved items"', self.header_html(self.home()))

    def test_the_heart_does_not_depend_on_the_account_icon_or_a_profile_picture(self):
        """On an edited header with an account icon and a Profile picture block, the heart shows on its own."""
        from . import section_sync as sync

        patched = sync.add_wishlist_block(edited_header())
        self.save_code(patched)
        header = self.section()
        self.assertEqual(set(header["spec"]["blocks"]), {"link", "profile", "wishlist"})
        values = header["values"]
        values["settings"]["show_account"] = False  # "Show account icon" is off
        values["blocks"] = [
            {"type": "profile", "settings": {"image": "", "size": 36, "url": "/account"}},
            self.wishlist_block(),
        ]
        _, preview = self.save_values(values, header["version"])
        for html in (preview, self.header_html(self.home())):
            self.assertEqual(self.hearts(html), 1)
            self.assertIn('class="site-header__profile"', html)  # the profile picture is there too
            self.assertNotIn("site-header__account", html)  # and the account icon is off
        # with the account icon on and no profile picture, the heart is still there
        values["settings"]["show_account"] = True
        values["blocks"] = [self.wishlist_block()]
        _, preview = self.save_values(values)
        self.assertEqual(self.hearts(preview), 1)
        self.assertIn("site-header__account", preview)

    def test_broken_wishlist_markup_is_refused_with_its_line_and_the_header_keeps_working(self):
        values = self.section()["values"]
        values["blocks"] = [self.wishlist_block()]
        self.save_values(values)
        good = self.code()
        bad = good.replace("{%- if block.type == 'wishlist' -%}", "{%- if block.type == 'wishlist' -%}{%- if -%}", 1)
        error = self.save_code(bad, expect=400)
        self.assertIn("Line", str(error["content"]))
        self.assertEqual(self.code(), good)  # the previous version keeps working
        self.assertEqual(self.hearts(self.header_html(self.home())), 1)

    def cart_block(self, **settings):
        return {"type": "cart", "settings": settings}

    def cart_icons(self, html):
        return html.count('class="site-header__cart"')

    def shopper_fragment(self, session, extra_headers=None):
        """The cart's lines as the page's script asks for them (a shopper's browser, not the owner's)."""
        from rest_framework.test import APIClient

        return APIClient().get(f"/s/{self.store['storeSlug']}/cart/fragment", HTTP_X_CART_SESSION=session).json()

    def test_cart_icon_is_in_the_add_block_list_with_its_options(self):
        spec = self.section()["spec"]["blocks"]["cart"]
        self.assertEqual(spec["name"], "Cart icon")
        self.assertEqual(
            set(spec["settings"]),
            {"label", "show_count", "opens", "size", "drawer_title", "checkout_label", "checkout_url",
             "empty_message", "show_discount", "show_login", "login_url"},
        )
        self.assertEqual(spec["settings"]["opens"]["options"], ["drawer", "page"])
        self.assertEqual(self.cart_icons(self.header_html(self.home())), 1)  # a new store starts with the cart icon
        self.assertIn("{% render 'cart', mode: 'icon'", self.code())  # the header only calls the snippet

    def test_changing_the_cart_icon_in_the_customizer_updates_the_code_the_store_and_the_cart(self):
        from rest_framework.test import APIClient

        header = self.section()
        values = header["values"]
        cart = next(b for b in values["blocks"] if b["type"] == "cart")
        cart["settings"].update(opens="page", size=30, drawer_title="Your bag", checkout_label="Pay now",
                                show_count=False, empty_message="Nothing here yet")
        _, preview = self.save_values(values, header["version"])
        self.assertIn('data-cart-opens="page"', preview)
        self.assertIn("width:30px;height:30px", preview)
        self.assertNotIn('<span class="site-header__cart-count"', preview)  # "Show the number of items" is off
        self.assertNotIn('<div class="cart-drawer"', preview)  # the icon opens the cart page: no drawer
        written = next(b for b in self.schema_of(self.code())["default"]["blocks"] if b["type"] == "cart")
        self.assertEqual(
            (written["settings"]["opens"], written["settings"]["size"], written["settings"]["drawer_title"]),
            ("page", 30, "Your bag"),
        )
        home = self.home()
        self.assertIn('data-cart-opens="page"', self.header_html(home))

        # the cart's own words come from the same block, so the cart page and the lines use them
        shopper = APIClient()
        page = shopper.get(f"/s/{self.store['storeSlug']}/cart").content.decode()
        self.assertIn("Nothing here yet", page)

    def test_removing_adding_reordering_and_hiding_the_cart_block_in_the_customizer(self):
        header = self.section()
        values = header["values"]
        values["blocks"] = [b for b in values["blocks"] if b["type"] != "cart"]
        _, preview = self.save_values(values, header["version"])
        self.assertEqual(self.cart_icons(preview), 0)
        self.assertNotIn("cart", self.default_block_types())
        self.assertEqual(self.cart_icons(self.header_html(self.home())), 0)

        values["blocks"].append(self.cart_block())
        _, preview = self.save_values(values)
        self.assertEqual(self.cart_icons(preview), 1)
        self.assertEqual(self.default_block_types(), ["wishlist", "cart"])
        self.assertEqual(self.cart_icons(self.header_html(self.home())), 1)

        values["blocks"] = [self.cart_block(), self.wishlist_block()]  # reorder: the cart first
        _, preview = self.save_values(values)
        self.assertEqual(self.default_block_types(), ["cart", "wishlist"])
        self.assertLess(preview.index('class="site-header__cart"'), preview.index('class="site-header__wishlist"'))
        values["blocks"] = [self.wishlist_block(), self.cart_block()]  # and back
        _, preview = self.save_values(values)
        self.assertLess(preview.index('class="site-header__wishlist"'), preview.index('class="site-header__cart"'))
        values["blocks"] = [self.cart_block(), self.wishlist_block()]
        self.save_values(values)

        values["blocks"][0]["hidden"] = True  # hide it: kept in the code, not shown
        _, preview = self.save_values(values)
        self.assertEqual(self.cart_icons(preview), 0)
        self.assertTrue(self.schema_of(self.code())["default"]["blocks"][0].get("hidden"))
        self.assertEqual(self.hearts(preview), 1)  # the wishlist icon does not depend on the cart icon

    def test_removing_and_adding_the_cart_block_type_in_code_updates_add_block_the_sidebar_and_the_store(self):
        from . import section_sync as sync

        without = self.with_schema(self.code(), lambda s: (
            s.update(blocks=[b for b in s["blocks"] if b["type"] != "cart"]),
            s["default"].update(blocks=[b for b in s["default"]["blocks"] if b["type"] != "cart"]),
        ))
        saved = self.save_code(without)
        self.assertNotIn("cart", saved["sync"]["spec"]["blocks"])  # gone from "Add block"
        self.assertNotIn("cart", [b["type"] for b in self.section()["values"]["blocks"]])  # and from the sidebar
        self.assertEqual(self.cart_icons(self.header_html(self.home())), 0)

        restored = sync.add_cart_block(self.code())
        self.assertIsNotNone(restored)
        saved = self.save_code(restored)
        self.assertEqual(saved["sync"]["spec"]["blocks"]["cart"]["name"], "Cart icon")  # offered again
        self.assertEqual(self.cart_icons(self.header_html(self.home())), 0)  # offered, not added
        current = self.section()
        values = current["values"]
        values["blocks"] = values["blocks"] + [self.cart_block(label="My bag")]
        _, preview = self.save_values(values, current["version"])
        self.assertIn('aria-label="My bag"', preview)
        self.assertIn('aria-label="My bag"', self.header_html(self.home()))

    def test_the_cart_icon_and_the_wishlist_icon_do_not_depend_on_each_other_or_the_account_icon(self):
        from . import section_sync as sync

        patched = sync.add_cart_block(sync.add_wishlist_block(edited_header()))
        self.save_code(patched)
        header = self.section()
        self.assertEqual(set(header["spec"]["blocks"]), {"link", "profile", "wishlist", "cart"})
        values = header["values"]
        values["settings"]["show_account"] = False
        values["blocks"] = [{"type": "profile", "settings": {"image": "", "size": 36, "url": "/account"}}, self.cart_block()]
        _, preview = self.save_values(values, header["version"])
        self.assertEqual(self.cart_icons(preview), 1)
        self.assertIn('class="site-header__profile"', preview)
        self.assertNotIn("site-header__account", preview)

    def test_broken_cart_markup_is_refused_with_its_line_and_the_header_keeps_working(self):
        good = self.code()
        bad = good.replace("{%- elsif block.type == 'cart' -%}", "{%- elsif block.type == 'cart' -%}{%- if -%}", 1)
        error = self.save_code(bad, expect=400)
        self.assertIn("Line", str(error["content"]))
        self.assertEqual(self.code(), good)
        self.assertEqual(self.cart_icons(self.header_html(self.home())), 1)

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
        self.assertEqual([b["type"] for b in values["blocks"]], ["wishlist", "cart"])  # only the icons: no menu links
        values["settings"]["collections"] = 2
        _, preview = self.save_values(values)
        nav = self.header_html(self.home())
        self.assertIn(">Home</a>", nav)
        self.assertIn(">Shop all</a>", nav)
        shown = [name for name in ("Autumn", "Basics", "Coats") if f">{name}</a>" in nav]
        self.assertEqual(shown, ["Autumn", "Basics"])
        # The fast preview path reads the collections the same way
        self.assertEqual(shown, [name for name in ("Autumn", "Basics", "Coats") if f">{name}</a>" in preview])

    def test_menu_from_admin_menus_shows_on_the_navbar(self):
        self.ok("post", "/api/v1/themes/menus/", {"name": "Main menu", "items": [
            {"title": "Sale", "url": "/collections/sale"}, {"title": "Journal", "url": "/blog"}]}, expect=201)
        nav = self.header_html(self.home())
        self.assertIn(">Sale</a>", nav)
        self.assertIn(">Journal</a>", nav)
        self.assertLess(nav.index(">Sale</a>"), nav.index(">Journal</a>"))
        self.assertNotIn(">Shop all</a>", nav)

    def test_new_stores_have_no_menus_and_the_navbar_has_home_and_shop_all(self):
        self.assertEqual(self.ok("get", "/api/v1/themes/menus/"), [])
        nav = self.header_html(self.home())
        self.assertIn(">Home</a>", nav)
        self.assertIn(">Shop all</a>", nav)

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
        self.assertEqual([b["settings"]["label"] for b in values["blocks"]], ["About", "Sale", "Wishlist", "Cart"])
        self.assertEqual([b["type"] for b in values["blocks"]], ["link", "link", "wishlist", "cart"])
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


class PromotionsSectionTest(StoreClientMixin, APITestCase):
    def setUp(self):
        self.token, self.store = self.sign_up("promotions@example.com", "Promotions Store")

    def test_promotions_is_available_for_home_without_changing_default_layout(self):
        schema = self.ok("get", "/api/v1/themes/schema/")
        self.assertEqual(schema["sections"]["promotions"]["pages"], ["index"])
        self.assertEqual(schema["sections"]["promotions"]["type_limits"]["promotion"], 8)
        self.assertIn("promotion", schema["sections"]["promotions"]["blocks"])

        settings = self.ok("get", "/api/v1/themes/settings/")
        self.assertEqual(settings["templates"]["index"]["order"],
                         ["slideshow", "featured", "carousel", "hero", "about"])
        self.assertNotIn("promotions", settings["templates"]["index"]["sections"])

        files = self.ok("get", "/api/v1/themes/files/")["files"]
        self.assertIn("sections/promotions.liquid", {item["path"] for item in files})

    def test_existing_theme_gets_only_the_missing_promotions_file(self):
        from apps.tenancy.models import Store
        from .services import get_store_theme

        store = Store.objects.get(store_id=self.store["storeId"])
        theme = get_store_theme(store).theme
        theme.files.filter(file_path="sections/promotions.liquid").delete()
        hero = theme.files.get(file_path="sections/hero.liquid")
        hero_content = hero.content
        get_store_theme(store)

        self.assertTrue(theme.files.filter(file_path="sections/promotions.liquid").exists())
        self.assertEqual(theme.files.get(file_path="sections/hero.liquid").content, hero_content)

    def test_promotions_schema_metadata_is_not_rendered_as_liquid(self):
        from apps.tenancy.models import Store
        from .services import get_store_theme

        store = Store.objects.get(store_id=self.store["storeId"])
        theme = get_store_theme(store).theme
        promotions = theme.files.get(file_path="sections/promotions.liquid")
        promotions.content += '\n{% schema %}\n{"name": "Promotions"}\n{% endschema %}\n'
        promotions.save(update_fields=["content", "updated_at"])

        settings = self.ok("get", "/api/v1/themes/settings/")
        index = settings["templates"]["index"]
        index["order"].append("promotions-test")
        index["sections"]["promotions-test"] = {"type": "promotions", "settings": {}}
        self.ok("put", "/api/v1/themes/settings/", settings)

        response = self.client.get(f"/s/{self.store['storeSlug']}/")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('class="section promotions"', html)
        self.assertNotIn("Liquid error in sections/promotions.liquid", html)

    def test_promotions_can_be_saved_previewed_and_rendered(self):
        product = self.ok("post", "/api/v1/catalog/products/", {
            "title": "Seasonal promotional item",
            "sku": "PROMO-ITEM-1",
            "status": "active",
            "price": "19.99",
        }, expect=201)
        collection = self.ok("post", "/api/v1/catalog/collections/", {
            "name": "Seasonal Promotions",
            "status": "active",
            "products": [product["id"]],
        }, expect=201)

        settings = self.ok("get", "/api/v1/themes/settings/")
        index = settings["templates"]["index"]
        section_id = "spring-offers"
        index["order"].append(section_id)
        index["sections"][section_id] = {
            "type": "promotions",
            "settings": {
                "heading": "Seasonal offers",
                "columns": "3",
                "view_all_collection": collection["id"],
            },
            "blocks": [{
                "type": "promotion",
                "settings": {
                    "heading": "Spring <script>alert(1)</script>",
                    "text": "Save on seasonal favorites.",
                    "image_url": "https://example.com/spring.jpg",
                    "image_alt": "Spring collection",
                    "discount_text": "15% off",
                    "button_label": "Shop Offers",
                    "button_link": "http://localhost:5173/",
                    "background_color": "#F2F4F7",
                },
            }, {
                "type": "button",
                "settings": {
                    "label": "Shop Offers",
                    "link": "http://localhost:5173/",
                },
            }],
        }
        self.ok("put", "/api/v1/themes/settings/", settings)

        saved = self.ok("get", "/api/v1/themes/settings/")
        self.assertEqual(saved["templates"]["index"]["order"][-1], section_id)
        preview = self.ok("post", "/api/v1/themes/preview/", saved)["html"]
        self.assertIn("Seasonal offers", preview)
        self.assertIn("15% off", preview)
        self.assertIn("Save on seasonal favorites.", preview)
        self.assertIn("https://example.com/spring.jpg", preview)
        promotions_url = f'/s/{self.store["storeSlug"]}/collections/{collection["handle"]}'
        self.assertIn(f'href="{promotions_url}">View all &rarr;</a>', preview)
        shop_offers_url = f'/s/{self.store["storeSlug"]}/collections/promotions'
        self.assertIn(f'href="{shop_offers_url}">Shop Offers</a>', preview)
        self.assertNotIn("localhost:5173", preview)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", preview)
        self.assertNotIn("<script>alert(1)</script>", preview)

        response = self.client.get(f"/s/{self.store['storeSlug']}/")
        self.assertEqual(response.status_code, 200)
        home_html = response.content.decode()
        self.assertIn("Seasonal offers", home_html)
        self.assertIn(f'href="{promotions_url}">View all &rarr;</a>', home_html)

        promotion_page = self.client.get(promotions_url)
        self.assertEqual(promotion_page.status_code, 200)
        self.assertIn("Seasonal Promotions", promotion_page.content.decode())
        self.assertIn("Seasonal promotional item", promotion_page.content.decode())

    def test_shop_offers_route_shows_welcome_then_promotional_collection_products(self):
        product = self.ok("post", "/api/v1/catalog/products/", {
            "title": "Promotional item",
            "sku": "PROMO-ROUTE-1",
            "status": "active",
            "price": "19.99",
        }, expect=201)
        promotion_url = f'/s/{self.store["storeSlug"]}/collections/promotions'

        empty_page = self.client.get(promotion_url)
        self.assertEqual(empty_page.status_code, 200)
        empty_html = empty_page.content.decode()
        self.assertIn("Welcome! Promotional products and special offers will appear here soon.", empty_html)
        self.assertNotIn("Promotional item", empty_html)

        self.ok("post", "/api/v1/catalog/collections/", {
            "name": "Promotions",
            "status": "active",
            "products": [product["id"]],
        }, expect=201)
        populated_page = self.client.get(promotion_url)
        self.assertEqual(populated_page.status_code, 200)
        self.assertIn("Promotional item", populated_page.content.decode())

    def test_promotions_cannot_be_added_to_non_home_pages(self):
        settings = self.ok("get", "/api/v1/themes/settings/")
        product = settings["templates"]["product"]
        section_id = "product-promotion"
        product["order"].append(section_id)
        product["sections"][section_id] = {"type": "promotions", "settings": {}}

        response = self.client.put("/api/v1/themes/settings/", settings, format="json")
        self.assertEqual(response.status_code, 400)


class PromotionsSyncTest(StoreClientMixin, APITestCase):
    """
    A Home page Promotions section in both editors, like the Product grid ("All products"): its values
    are its entry in templates/index.json in Edit code and its panel in the customizer (one saved copy);
    its line in templates/index.liquid follows the sections list; its markup is sections/promotions.liquid,
    which neither editor's value changes ever rewrite.
    """

    PROMOTIONS = "sections/promotions.liquid"
    INDEX_JSON = "templates/index.json"
    INDEX_LIQUID = "templates/index.liquid"

    def setUp(self):
        self.token, self.store = self.sign_up("promo-sync@example.com", "Promo Sync Store")
        # Added in the customizer, like a merchant picking it from "Add section"
        data = self.ok("get", "/api/v1/themes/settings/")
        index = data["templates"]["index"]
        self.ok("patch", "/api/v1/themes/settings/", {"templates": {"index": {
            "order": index["order"] + ["offers"],
            "sections": {"offers": {"type": "promotions", "settings": {"heading": "Weekly offers"},
                                    "blocks": self.cards("Card A", "Card B")}},
        }}})

    def cards(self, *headings):
        return [{"type": "promotion", "settings": {"heading": h, "discount_text": f"{h} deal"}} for h in headings]

    def files(self):
        return {f["path"]: f["content"] for f in self.ok("get", "/api/v1/themes/files/")["files"]}

    def save_file(self, path, content, expect=200):
        r = self.client.post("/api/v1/themes/files/", {"path": path, "content": content}, format="json")
        self.assertEqual(r.status_code, expect, r.content[:500])
        return r.json()

    def code_index(self):
        return json.loads(self.files()[self.INDEX_JSON])

    def save_code_index(self, change):
        index = self.code_index()
        change(index["sections"]["offers"])
        self.save_file(self.INDEX_JSON, json.dumps(index, indent=2))

    def customizer_offers(self):
        return self.ok("get", "/api/v1/themes/settings/")["templates"]["index"]["sections"]["offers"]

    def save_customizer_offers(self, change):
        data = self.ok("get", "/api/v1/themes/settings/")
        section = data["templates"]["index"]["sections"]["offers"]
        change(section)
        self.ok("patch", "/api/v1/themes/settings/", {"templates": {"index": {
            "order": data["templates"]["index"]["order"], "sections": {"offers": section}}}})

    def store_html(self):
        r = self.client.get(f"/s/{self.store['storeSlug']}/")
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def preview_html(self):
        data = self.ok("get", "/api/v1/themes/settings/")
        return self.ok("post", "/api/v1/themes/preview/?section=offers", data)["html"]

    def card_headings(self, section):
        return [b["settings"]["heading"] for b in section["blocks"]]

    def test_1_code_setting_reaches_customizer_preview_and_store(self):
        self.save_code_index(lambda s: s["settings"].update(heading="From code", columns="2"))
        self.assertEqual(self.customizer_offers()["settings"]["heading"], "From code")
        self.assertEqual(self.customizer_offers()["settings"]["columns"], "2")
        for html in (self.preview_html(), self.store_html()):
            self.assertIn("From code", html)
            self.assertIn("--promotions-columns: 2", html)

    def test_2_customizer_setting_reaches_code_preview_and_store(self):
        def change(s):
            s["settings"].update(heading="From sidebar", columns="4")
            s["blocks"][0]["settings"].update(discount_text="30% off", button_label="Shop now", button_link="/collections/all",
                                              image_url="https://example.com/sale.jpg", background_color="#112233")
        self.save_customizer_offers(change)

        offers = self.code_index()["sections"]["offers"]  # written to the code, in the same data format
        self.assertEqual(offers["settings"]["heading"], "From sidebar")
        self.assertEqual(offers["settings"]["columns"], "4")
        card = offers["blocks"][0]["settings"]
        self.assertEqual((card["discount_text"], card["button_label"], card["button_link"], card["image_url"],
                          card["background_color"]),
                         ("30% off", "Shop now", "/collections/all", "https://example.com/sale.jpg", "#112233"))
        for html in (self.preview_html(), self.store_html()):
            self.assertIn("From sidebar", html)
            self.assertIn("30% off", html)
            self.assertIn("https://example.com/sale.jpg", html)
            self.assertIn("background-color: #112233", html)
            self.assertIn(f'href="/s/{self.store["storeSlug"]}/collections/all">Shop now</a>', html)

    def test_3_cards_add_reorder_hide_remove_in_both_editors(self):
        # Added in the code
        self.save_code_index(lambda s: s["blocks"].extend(self.cards("Card C")))
        self.assertEqual(self.card_headings(self.customizer_offers()), ["Card A", "Card B", "Card C"])
        self.assertIn("Card C", self.store_html())
        # Reordered in the customizer
        self.save_customizer_offers(lambda s: s["blocks"].reverse())
        self.assertEqual(self.card_headings(self.code_index()["sections"]["offers"]), ["Card C", "Card B", "Card A"])
        html = self.store_html()
        self.assertLess(html.index("Card C"), html.index("Card A"))
        # Hidden in the customizer, shown again in the code
        self.save_customizer_offers(lambda s: s["blocks"][0].update(hidden=True))
        self.assertTrue(self.code_index()["sections"]["offers"]["blocks"][0]["hidden"])
        self.assertNotIn("Card C", self.store_html())
        self.save_code_index(lambda s: s["blocks"][0].update(hidden=False))
        self.assertFalse(self.customizer_offers()["blocks"][0].get("hidden"))
        self.assertIn("Card C", self.preview_html())
        # Removed in the code
        self.save_code_index(lambda s: s["blocks"].pop(0))
        self.assertEqual(self.card_headings(self.customizer_offers()), ["Card B", "Card A"])
        self.assertNotIn("Card C", self.store_html())

    def test_4_adding_the_section_in_either_editor_updates_the_other(self):
        # Added in the customizer (setUp): its line is written into templates/index.liquid
        self.assertIn("offers", [t["id"] for t in sync.template_tags(self.files()[self.INDEX_LIQUID])])
        # Added in the code: a new line creates a Promotions section with its default settings
        code = self.files()[self.INDEX_LIQUID]
        line = "{% section 'offers', type: 'promotions' %}"
        self.assertIn(line, code)
        self.save_file(self.INDEX_LIQUID, code.replace(line, line + "\n{% section 'more-offers', type: 'promotions' %}"))
        index = self.ok("get", "/api/v1/themes/settings/")["templates"]["index"]
        self.assertEqual(index["order"][index["order"].index("offers") + 1], "more-offers")
        self.assertEqual(index["sections"]["more-offers"]["type"], "promotions")
        self.assertEqual(index["sections"]["more-offers"]["settings"]["heading"], "Promotions")
        self.assertEqual(self.store_html().count('class="section promotions"'), 2)

    def test_5_custom_markup_survives_both_editors(self):
        edited = self.files()[self.PROMOTIONS].replace(
            '<section id="section-{{ section.id }}"', '<p class="promo-note">Hand made</p><section id="section-{{ section.id }}"', 1)
        self.assertNotEqual(edited, self.files()[self.PROMOTIONS])
        self.save_file(self.PROMOTIONS, edited)
        self.save_customizer_offers(lambda s: s["settings"].update(heading="Sidebar"))
        self.save_code_index(lambda s: s["settings"].update(heading="Code"))
        self.assertEqual(self.files()[self.PROMOTIONS], edited)  # neither value change rewrote the markup
        self.assertIn('class="promo-note"', self.preview_html())
        self.assertIn('class="promo-note"', self.store_html())

    def test_6_invalid_promotions_code_is_refused_and_store_keeps_working(self):
        saved = self.files()[self.PROMOTIONS]
        r = self.save_file(self.PROMOTIONS, saved.replace("{%- endif -%}", "", 1), expect=400)
        self.assertIn("Line", r["content"][0] if isinstance(r["content"], list) else r["content"])
        self.assertEqual(self.files()[self.PROMOTIONS], saved)
        html = self.store_html()
        self.assertNotIn("Liquid error", html)
        self.assertIn("Weekly offers", html)


class PromotionsProductsTest(StoreClientMixin, APITestCase):
    """
    Promotions shows the products of the collection chosen in the customizer, with the shared product
    cards, under its promotion cards. The collection is read on every render; nothing is copied into the theme.
    """

    INDEX_JSON = "templates/index.json"

    def setUp(self):
        self.token, self.store = self.sign_up("promo-products@example.com", "Promo Products Store")
        self.a = self.product("Product A", "500", images=["https://example.com/a.jpg"])
        self.b = self.product("Product B", "800")
        self.c = self.product("Product C", "1000")  # no image
        self.collection = self.ok("post", "/api/v1/catalog/collections/", {
            "name": "Promotions", "status": "active", "products": [self.a["id"], self.c["id"]],
        }, expect=201)
        self.save_offers({"heading": "Deals", "view_all_collection": self.collection["id"]},
                         [{"type": "promotion", "settings": {"heading": "Card deal", "discount_text": "10% off"}}])

    def product(self, title, price, images=()):
        return self.ok("post", "/api/v1/catalog/products/", {
            "title": title, "sku": title.replace(" ", "-").upper(), "status": "active", "price": price,
            "images": list(images),
        }, expect=201)

    def save_offers(self, settings, blocks=None):
        data = self.ok("get", "/api/v1/themes/settings/")
        index = data["templates"]["index"]
        section = index["sections"].get("offers", {"type": "promotions", "settings": {}, "blocks": []})
        section["settings"].update(settings)
        if blocks is not None:
            section["blocks"] = blocks
        order = index["order"] if "offers" in index["order"] else index["order"] + ["offers"]
        self.ok("patch", "/api/v1/themes/settings/", {"templates": {"index": {"order": order, "sections": {"offers": section}}}})

    def offers(self):
        return self.ok("get", "/api/v1/themes/settings/")["templates"]["index"]["sections"]["offers"]

    def promotions_html(self, html=None):
        """Just the Promotions section (the home page's All products grid lists every product too)."""
        if html is None:
            r = self.client.get(f"/s/{self.store['storeSlug']}/")
            self.assertEqual(r.status_code, 200)
            html = r.content.decode()
        self.assertNotIn("Liquid error", html)
        start = html.index('class="section promotions"')
        return html[start:html.index("</section>", start)]

    def preview_html(self):
        data = self.ok("get", "/api/v1/themes/settings/")
        return self.ok("post", "/api/v1/themes/preview/?section=offers", data)["html"]

    def cards(self, html):
        return html.count('class="product-card-shell"')

    def url(self, product):
        return f'/s/{self.store["storeSlug"]}/products/{product["id"]}'

    def test_selected_collection_shows_only_its_products_with_title_image_price_and_link(self):
        for html in (self.promotions_html(), self.promotions_html(self.preview_html())):
            self.assertEqual(self.cards(html), 2)
            self.assertIn("Product A", html)
            self.assertIn("Product C", html)
            self.assertNotIn("Product B", html)
            self.assertIn('src="https://example.com/a.jpg"', html)
            self.assertIn("500.00</span>", html)  # the product's own price: nothing is discounted for being promoted
            self.assertIn("1,000.00</span>", html)
            self.assertIn(f'href="{self.url(self.a)}"', html)
            self.assertIn(f'href="{self.url(self.c)}"', html)
            self.assertIn("Card deal", html)  # the promotion cards stay
            self.assertIn("10% off", html)

    def test_adding_and_removing_collection_products_shows_on_the_next_render(self):
        self.ok("patch", f"/api/v1/catalog/collections/{self.collection['id']}/",
                {"products": [self.c["id"], self.b["id"]]})
        html = self.promotions_html()
        self.assertIn("Product B", html)
        self.assertNotIn("Product A", html)
        self.assertIn("Product C", html)
        self.assertNotIn(self.b["id"], json.dumps(self.offers()))  # nothing about the products is saved in the theme

    def test_view_all_opens_the_selected_collection_page(self):
        page_url = f'/s/{self.store["storeSlug"]}/collections/{self.collection["handle"]}'
        self.assertIn(f'href="{page_url}">View all &rarr;</a>', self.promotions_html())
        page = self.client.get(page_url)
        self.assertEqual(page.status_code, 200)
        html = page.content.decode()
        self.assertIn("Product A", html)
        self.assertIn("Product C", html)
        self.assertNotIn("Product B", html)

    def test_product_limit_and_columns(self):
        self.save_offers({"product_limit": 1, "product_columns": 3, "product_columns_mobile": 1})
        html = self.promotions_html()
        self.assertEqual(self.cards(html), 1)
        self.assertIn("--cols: 3; --cols-mobile: 1", html)

    def test_empty_or_no_collection_is_safe_and_cards_only_stay_as_before(self):
        empty = self.ok("post", "/api/v1/catalog/collections/", {"name": "Empty offers", "status": "active"}, expect=201)
        self.save_offers({"view_all_collection": empty["id"]})
        html = self.promotions_html()
        self.assertEqual(self.cards(html), 0)
        self.assertIn("No products yet.", html)
        # No collection: only the promotion cards, as before this feature
        self.save_offers({"view_all_collection": ""})
        html = self.promotions_html()
        self.assertEqual(self.cards(html), 0)
        self.assertNotIn("No products yet.", html)
        self.assertNotIn("View all", html)
        self.assertIn("Card deal", html)

    def test_products_hidden_from_the_store_are_left_out(self):
        self.ok("patch", f"/api/v1/catalog/products/{self.a['id']}/", {"status": "draft"})
        html = self.promotions_html()
        self.assertNotIn("Product A", html)
        self.assertIn("Product C", html)

    def test_settings_persist_and_sync_with_the_code_both_ways(self):
        # Customizer -> code
        self.save_offers({"product_limit": 2, "product_columns": 5})
        code = json.loads({f["path"]: f["content"] for f in self.ok("get", "/api/v1/themes/files/")["files"]}[self.INDEX_JSON])
        saved = code["sections"]["offers"]["settings"]
        self.assertEqual((saved["view_all_collection"], saved["product_limit"], saved["product_columns"]),
                         (self.collection["id"], 2, 5))
        # Code -> customizer, preview and store
        code["sections"]["offers"]["settings"].update(product_limit=1, product_columns_mobile=1)
        r = self.client.post("/api/v1/themes/files/", {"path": self.INDEX_JSON, "content": json.dumps(code)}, format="json")
        self.assertEqual(r.status_code, 200, r.content[:500])
        reloaded = self.offers()["settings"]
        self.assertEqual((reloaded["product_limit"], reloaded["product_columns"], reloaded["product_columns_mobile"]),
                         (1, 5, 1))
        self.assertEqual(reloaded["view_all_collection"], self.collection["id"])
        self.assertEqual(self.offers()["blocks"][0]["settings"]["heading"], "Card deal")
        for html in (self.promotions_html(), self.promotions_html(self.preview_html())):
            self.assertEqual(self.cards(html), 1)
            self.assertIn("--cols: 5; --cols-mobile: 1", html)

    def test_an_untouched_promotions_file_gets_the_product_grid_and_an_edited_one_is_kept(self):
        import hashlib

        from apps.tenancy.models import Store
        from .services import STARTER_UPGRADES, get_store_theme, starter_files

        new = starter_files()["sections/promotions.liquid"]
        start = new.index("    {%- comment -%} The chosen collection's products")
        css = "  #section-{{ section.id }} .promotions__grid + .promotions__products {\n    margin-top: 32px;\n  }\n"
        old = (new[:start] + new[new.index("  </div>\n</section>"):]).replace(css, "")
        self.assertIn(hashlib.sha256(old.encode("utf-8")).hexdigest(), STARTER_UPGRADES["sections/promotions.liquid"])

        store = Store.objects.get(store_id=self.store["storeId"])
        for content, expected in ((old, new), (old + "{%- comment -%} mine {%- endcomment -%}\n", None)):
            theme = get_store_theme(store).theme
            theme.files.filter(file_path="sections/promotions.liquid").update(content=content)
            theme.theme_data = {**theme.theme_data, "starter_revision": 35}
            theme.save()
            get_store_theme(store)
            stored = theme.files.get(file_path="sections/promotions.liquid").content
            self.assertEqual(stored, expected or content)


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

        from .services import _add_header_wishlist_setting

        content = legacy_header()
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
        content = legacy_header().replace(
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


class HeaderWishlistBlockPatchTest(StoreClientMixin, APITestCase):
    """An existing header gets the "Wishlist icon" block by inserting only; the owner's edits are never lost."""

    def setUp(self):
        self.token, self.store = self.sign_up("headerpatch@example.com", "Patch Store")
        self.base = f"/s/{self.store['storeSlug']}"

    def theme(self):
        from .services import get_store_theme
        from apps.tenancy.models import Store

        return get_store_theme(Store.objects.get(store_id=self.store["storeId"]))

    def stored(self):
        from .models import Theme

        return Theme.objects.get(slug=f"store-{self.store['storeId']}").files.get(file_path="sections/header.liquid").content

    def install(self, content):
        """The store's header is now `content`, and the store has not had the one-time patch yet."""
        theme = self.theme().theme
        theme.files.filter(file_path="sections/header.liquid").update(content=content, file_size=len(content.encode("utf-8")))
        theme.theme_data = {k: v for k, v in theme.theme_data.items() if k != "wishlist_icon_offered"}
        theme.save()

    def test_a_header_without_the_block_gets_the_type_and_the_loop_and_nothing_else_changes(self):
        from . import section_sync as sync

        header = edited_header()
        patched = sync.add_wishlist_block(header)
        self.assertIsNotNone(patched)
        sync.parse(patched, "sections/header.liquid")
        spec = sync.spec_of("header", sync.parse(patched))
        self.assertEqual(set(spec["blocks"]), {"link", "profile", "wishlist"})
        # only insertions: taking them out gives the owner's header back, character for character
        undone = patched.replace(sync.WISHLIST_BLOCK_MARKUP, "").replace(",\n    " + sync.WISHLIST_BLOCK_SCHEMA, "")
        self.assertEqual(undone, header)
        self.assertIn("owner note: keep this line", patched)
        self.assertIn("site-header__account", patched)  # the account icon and the profile picture stay
        self.assertIn("{% render 'wishlist', mode: 'icon'", patched)
        self.assertIsNone(sync.add_wishlist_block(patched))  # done once: nothing more to add

    def test_a_header_that_renders_the_heart_but_lacks_the_block_type_gets_only_the_type(self):
        from . import section_sync as sync

        with_loop = sync.add_wishlist_block(edited_header())
        no_type = with_loop.replace(",\n    " + sync.WISHLIST_BLOCK_SCHEMA, "")
        self.assertNotIn('"type": "wishlist"', no_type)  # like a store whose block type was removed from the schema
        patched = sync.add_wishlist_block(no_type)
        self.assertEqual(patched, with_loop)  # the loop is not added a second time

    def test_crlf_headers_keep_their_line_endings(self):
        from . import section_sync as sync

        header = edited_header().replace("\n", "\r\n")
        patched = sync.add_wishlist_block(header)
        self.assertIsNotNone(patched)
        self.assertNotIn("\r\r\n", patched)
        self.assertEqual(patched.count("\n"), patched.count("\r\n"))
        sync.parse(patched, "sections/header.liquid")

    def test_a_header_it_cannot_patch_safely_is_left_alone(self):
        from . import section_sync as sync

        header = edited_header()
        self.assertIsNone(sync.add_wishlist_block(header.replace("</header>", "")))  # no closing tag to insert before
        self.assertIsNone(sync.add_wishlist_block(header.replace("{% endschema %}", "")))  # no schema
        broken = header.replace('"max_blocks": 12,', '"max_blocks": 12 ,,')
        self.assertIsNone(sync.add_wishlist_block(broken))  # the schema is not valid JSON
        self.assertIsNone(sync.add_wishlist_block(header.replace('"blocks": [', '"things": [', 1)))  # no blocks list

    def test_the_store_upgrade_patches_an_edited_header_once_and_keeps_its_history(self):
        from . import section_sync as sync

        header = edited_header()
        self.install(header)
        store_theme = self.theme()
        patched = self.stored()
        self.assertNotEqual(patched, header)
        self.assertIn("owner note: keep this line", patched)
        self.assertIn("wishlist", [b["type"] for b in sync.parse(patched)["blocks"]])
        self.assertEqual(sync.history_of(store_theme.theme, "header")[0]["preview"], header[:160])  # the old code can be restored
        # the sections list now offers the block
        self.assertIn("wishlist", self.ok("get", "/api/v1/themes/sections/")["header"]["spec"]["blocks"])

        # an owner who removes the block type later does not get it back
        removed = patched.replace(",\n    " + sync.WISHLIST_BLOCK_SCHEMA, "").replace(sync.WISHLIST_BLOCK_MARKUP, "")
        self.ok("post", "/api/v1/themes/files/", {"path": "sections/header.liquid", "content": removed})
        self.theme()
        self.assertEqual(self.stored(), removed)

    def test_an_owner_edited_header_that_already_shows_the_heart_keeps_it(self):
        """The legacy "Show Wishlist" header: its heart, its setting and its markup stay as they are."""
        legacy = legacy_header() + "<!-- mine -->\n"
        self.install(legacy)
        store_theme = self.theme()
        # like a store that has always had it: the heart is the setting, there is no block saved
        store_theme.custom_data = {"sections": {"header": {"settings": {"show_wishlist": True}, "blocks": []}}}
        store_theme.save()
        patched = self.stored()
        self.assertIn("show_wishlist", patched)  # their own heart and its setting
        self.assertIn("<!-- mine -->", patched)
        home = self.client.get(f"{self.base}/").content.decode()
        header = home[home.index("<header"):home.index("</header>")]
        self.assertEqual(header.count('class="site-header__wishlist"'), 1)  # still one heart, not two

    def test_an_unpatchable_header_is_marked_done_and_not_retried_on_every_visit(self):
        broken = edited_header().replace("</header>", "")
        self.install(broken)
        self.theme()
        self.assertEqual(self.stored(), broken)
        self.assertEqual(self.theme().theme.theme_data["wishlist_icon_offered"], 1)


class CartTest(StoreClientMixin, APITestCase):
    """The cart icon, the drawer's lines and the cart page, for shoppers with no account."""

    def setUp(self):
        from rest_framework.test import APIClient

        self.token, self.store = self.sign_up("cartshop@example.com", "Cart Store")
        self.slug = self.store["storeSlug"]
        self.location = self.ok("post", "/api/v1/inventory/locations/", {"name": "Main"}, expect=201)["id"]
        self.mug = self.product("Clay mug", "MUG-1", 5)
        self.gone = self.product("Gone mug", "MUG-0", 0)
        self.shopper = APIClient()  # a visitor's browser, with its own cookies
        self.session = self.page_session(self.shopper, self.slug)

    def product(self, title, sku, quantity):
        return self.ok("post", "/api/v1/catalog/products/", {
            "title": title, "sku": sku, "status": "active", "price": "12", "location": self.location,
            "quantity": quantity,
        }, expect=201)

    def page_session(self, client, slug, path=""):
        """The cart id a page gives the shopper (in the page for the script, in a cookie for the next page)."""
        import re

        response = client.get(f"/s/{slug}/{path}")
        match = re.search(r'data-cart-session="([0-9a-f]{32})"', response.content.decode())
        self.assertIsNotNone(match, "the page has no cart id")
        return match.group(1)

    def call(self, method, path, body=None, session=None, client=None, slug=None):
        client = client or self.shopper
        kwargs = {"HTTP_X_CART_SESSION": self.session if session is None else session}
        url = f"/s/{slug or self.slug}/cart/{path}"
        if method == "get":
            return client.get(url, **kwargs)
        return client.post(url, json.dumps(body or {}), content_type="application/json", **kwargs)

    def add(self, product, quantity=1, **kw):
        return self.call("post", "add", {"product": product["id"], "quantity": quantity}, **kw)

    def header(self, client=None, path=""):
        html = (client or self.shopper).get(f"/s/{self.slug}/{path}").content.decode()
        return html[html.index("<header"):html.index("</header>")]

    def count_in(self, html):
        import re

        return int(re.search(r'data-cart-items="(\d+)"', html).group(1))

    def test_the_cart_preflight_from_the_sandboxed_storefront_is_allowed(self):
        # The storefront page has origin "null" and sends X-Cart-Session: the browser asks first (preflight)
        # and blocks "Add to cart" ("Failed to fetch") unless this answer carries the CORS headers.
        r = self.shopper.options(
            f"/s/{self.slug}/cart/add", HTTP_ORIGIN="null",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="content-type,x-cart-session",
        )
        self.assertEqual(r.status_code, 204)
        self.assertEqual(r["Access-Control-Allow-Origin"], "*")
        self.assertIn("X-Cart-Session", r["Access-Control-Allow-Headers"])

    def test_adding_to_the_cart_raises_the_header_count_each_time(self):
        self.assertEqual(self.count_in(self.header()), 0)
        first = self.add(self.mug)
        self.assertEqual((first.status_code, first.json()["count"]), (200, 1))
        self.assertEqual(self.add(self.mug).json()["count"], 2)  # again: up again
        header = self.header()  # the next page load shows it from the cookie, no script needed
        self.assertEqual(self.count_in(header), 2)
        self.assertIn('data-cart-count aria-live="polite">2</span>', header)

    def test_the_lines_the_total_plus_minus_and_remove(self):
        self.add(self.mug, 2)
        data = self.call("get", "fragment").json()
        self.assertEqual((data["count"], data["subtotal"]), (2, "24.00"))
        html = data["html"]
        self.assertIn("Clay mug", html)
        self.assertIn('class="cart-qty__value">2</span>', html)
        self.assertIn('data-quantity="1"', html)  # minus
        self.assertIn('data-quantity="3"', html)  # plus
        self.assertIn('data-quantity="0"', html)  # remove (the bin)
        self.assertIn("Estimated total", html)
        self.assertIn("Duties and taxes included. Shipping is calculated at checkout.", html)
        self.assertIn("Discount", html)
        self.assertIn("Check out", html)
        line = __import__("re").search(r'data-line="([0-9a-f-]{36})"', html).group(1)

        plus = self.call("post", "change", {"line": line, "quantity": 3}).json()
        self.assertEqual((plus["count"], plus["subtotal"]), (3, "36.00"))
        self.assertIn('class="cart-qty__value">3</span>', plus["html"])
        minus = self.call("post", "change", {"line": line, "quantity": 1}).json()
        self.assertEqual((minus["count"], minus["subtotal"]), (1, "12.00"))
        removed = self.call("post", "change", {"line": line, "quantity": 0}).json()
        self.assertEqual(removed["count"], 0)
        self.assertIn("Your cart is empty", removed["html"])
        self.assertEqual(self.count_in(self.header()), 0)

    def test_the_empty_cart_page_and_the_full_cart_page(self):
        page = self.shopper.get(f"/s/{self.slug}/cart").content.decode()
        self.assertIn("Your cart is empty", page)
        self.assertIn(f'href="/s/{self.slug}/collections/all">Continue shopping</a>', page)
        self.assertNotIn("Have an account?", page)  # off unless the owner turns it on
        # the icon is a link to the cart page, so it opens it with JavaScript off, and when the cart is empty
        self.assertIn(f'class="site-header__cart" href="/s/{self.slug}/cart"', self.header())
        self.assertEqual(self.shopper.get(f"/s/{self.slug}/cart/").status_code, 200)  # with or without the slash

        self.add(self.mug, 2)
        page = self.shopper.get(f"/s/{self.slug}/cart").content.decode()
        self.assertIn("Clay mug", page)
        self.assertIn("24.00", page)
        self.assertIn("Estimated total", page)
        self.assertNotIn("Your cart is empty", page)

    def test_stock_limit_and_sold_out_products_are_refused(self):
        self.assertEqual(self.add(self.mug, 5).json()["count"], 5)
        refused = self.add(self.mug, 1)
        self.assertEqual(refused.status_code, 400)
        self.assertIn("Only 5 of Clay mug left", refused.json()["error"])
        self.assertEqual(self.call("get", "fragment").json()["count"], 5)  # nothing was added
        line = __import__("re").search(r'data-line="([0-9a-f-]{36})"', self.call("get", "fragment").json()["html"]).group(1)
        over = self.call("post", "change", {"line": line, "quantity": 6})
        self.assertEqual(over.status_code, 400)
        self.assertEqual(self.call("get", "fragment").json()["count"], 5)
        gone = self.add(self.gone, 1)
        self.assertEqual(gone.status_code, 400)
        self.assertIn("sold out", gone.json()["error"])
        self.assertEqual(self.add(self.mug, 0).status_code, 400)  # not a quantity
        self.assertEqual(self.call("post", "add", {"product": self.mug["id"], "quantity": 1.5}).status_code, 400)
        self.assertEqual(self.call("post", "add", {"product": self.mug["id"], "quantity": 1000}).status_code, 400)

    def test_another_shopper_or_another_store_sees_nothing_of_this_cart(self):
        from rest_framework.test import APIClient

        self.add(self.mug, 2)
        line = __import__("re").search(r'data-line="([0-9a-f-]{36})"', self.call("get", "fragment").json()["html"]).group(1)

        stranger = APIClient()
        other = self.page_session(stranger, self.slug)
        self.assertNotEqual(other, self.session)
        seen = self.call("get", "fragment", session=other, client=stranger).json()
        self.assertEqual(seen["count"], 0)  # a different cart id: an empty cart
        self.assertNotIn("Clay mug", seen["html"])
        taken = self.call("post", "change", {"line": line, "quantity": 0}, session=other, client=stranger)
        self.assertEqual(taken.status_code, 400)  # another shopper's line is not theirs to change
        self.assertEqual(self.call("get", "fragment").json()["count"], 2)
        self.assertEqual(self.call("get", "fragment", session="abc", client=stranger).json()["count"], 0)  # not an id we made

        # another store: this cart id means nothing there, and its products cannot be put in this cart
        token, second = self.sign_up("othercartshop@example.com", "Other Cart Store")
        location = self.ok("post", "/api/v1/inventory/locations/", {"name": "Main"}, expect=201)["id"]
        theirs = self.ok("post", "/api/v1/catalog/products/", {
            "title": "Their mug", "sku": "T-1", "status": "active", "price": "5", "location": location, "quantity": 3,
        }, expect=201)
        other_slug = second["storeSlug"]
        self.assertEqual(self.call("get", "fragment", slug=other_slug).json()["count"], 0)  # same id, other store
        self.assertEqual(self.add(theirs, 1, slug=self.slug).status_code, 400)  # not a product of this store
        self.assertEqual(self.add(self.mug, 1, slug=other_slug).status_code, 400)
        self.assertEqual(self.call("get", "fragment").json()["count"], 2)

    def test_liquid_gets_a_cart_with_the_item_count_for_headers_that_read_it(self):
        """A header may say {% assign cart_count = cart.item_count | default: 0 %}: the count must be real, page after page."""
        marker = '<i data-test-cart-count="{{ cart.item_count }}"></i></header>'
        header = self.ok("get", "/api/v1/themes/files/")["files"]
        code = next(f["content"] for f in header if f["path"] == "sections/header.liquid")
        self.ok("post", "/api/v1/themes/files/", {"path": "sections/header.liquid", "content": code.replace("</header>", marker, 1)})
        self.assertIn('data-test-cart-count="0"', self.shopper.get(f"/s/{self.slug}/").content.decode())
        self.add(self.mug, 2)
        for path in ("", "cart", f"products/{self.mug['id']}"):
            self.assertIn('data-test-cart-count="2"', self.shopper.get(f"/s/{self.slug}/{path}").content.decode(), path)

    def preview_html(self, product=None, page="product"):
        """The customizer's preview of a page (what the owner sees while editing), as the admin asks for it."""
        data = self.ok("get", "/api/v1/themes/settings/")
        query = f"?page={page}" + (f"&product={product['id']}" if product else "")
        return self.ok("post", f"/api/v1/themes/preview/{query}", data)["html"]

    def preview_cart_id(self, html):
        import re

        found = re.findall(r'data-cart-session="([^"]*)"', html)
        self.assertTrue(found, "the preview has no cart on it")
        self.assertEqual(set(found), {found[0]})
        return found[0]

    def test_the_product_page_in_the_customizer_preview_has_a_working_cart(self):
        """The preview has no shopper, yet Add to cart must work there like on the store: it has its own test cart."""
        from rest_framework.test import APIClient

        html = self.preview_html(self.mug)
        cart_id = self.preview_cart_id(html)
        self.assertRegex(cart_id, r"^[0-9a-f]{32}$")  # never empty: no "The cart is not available" message
        self.assertIn("data-cart-add", html)
        self.assertIn(f'name="product" value="{self.mug["id"]}"', html)
        self.assertEqual(self.preview_cart_id(self.preview_html(self.mug)), cart_id)  # the same one every time
        self.assertEqual(self.preview_cart_id(self.preview_html(self.gone)), cart_id)  # for every product
        self.assertEqual(self.preview_cart_id(self.preview_html(page="index")), cart_id)  # and the other pages

        # quantity 2 from the preview's product page: the right product and quantity are in the cart
        browser = APIClient()
        added = self.call("post", "add", {"product": self.mug["id"], "quantity": 2}, session=cart_id, client=browser)
        self.assertEqual((added.status_code, added.json()["count"]), (200, 2))
        lines = self.call("get", "fragment", session=cart_id, client=browser).json()
        self.assertIn("Clay mug", lines["html"])
        self.assertIn('class="cart-qty__value">2</span>', lines["html"])
        self.assertEqual(lines["subtotal"], "24.00")
        # the preview then shows it in its header
        self.assertEqual(self.count_in(self.preview_html(self.mug)), 2)
        # it is the owner's test cart: no shopper sees it, and the stock limit still applies
        self.assertEqual(self.call("get", "fragment").json()["count"], 0)
        self.assertEqual(self.call("post", "add", {"product": self.mug["id"], "quantity": 9}, session=cart_id, client=browser).status_code, 400)

    def test_the_preview_cart_belongs_to_one_owner_and_one_store(self):
        mine = self.preview_cart_id(self.preview_html(self.mug))
        token, second = self.sign_up("othercartowner@example.com", "Another Store")
        location = self.ok("post", "/api/v1/inventory/locations/", {"name": "Main"}, expect=201)["id"]
        theirs = self.ok("post", "/api/v1/catalog/products/", {
            "title": "Another mug", "sku": "AN-1", "status": "active", "price": "5", "location": location, "quantity": 3,
        }, expect=201)
        other = self.preview_cart_id(self.preview_html(theirs))
        self.assertNotEqual(other, mine)  # another store and owner: another cart
        self.assertNotEqual(other, self.session)

    def test_every_products_page_has_the_same_working_add_to_cart_form(self):
        import re

        for product in (self.mug, self.gone):
            page = self.shopper.get(f"/s/{self.slug}/products/{product['id']}").content.decode()
            form = re.search(r'<form class="product__form"[^>]*>', page).group(0)
            self.assertIn("data-cart-add", form)
            self.assertRegex(form, r'data-cart-session="[0-9a-f]{32}"')  # a cart id on every product page
            self.assertIn(f'name="product" value="{product["id"]}"', page)
            self.assertIn('name="quantity"', page)  # the quantity field the script reads
            self.assertIn('data-notice></p>', page)  # no message is shown: the script's fallback is only for a page with no cart id

    def test_the_cart_is_not_available_message_is_only_for_a_page_with_no_cart_id(self):
        from .services import starter_files

        snippet = starter_files()["snippets/cart.liquid"]
        self.assertEqual(snippet.count("The cart is not available on this page."), 1)  # the one fallback in the script

    def test_a_cart_call_without_a_cart_id_is_refused_and_nothing_is_created(self):
        from apps.commerce.models import Cart

        before = Cart.objects.count()
        refused = self.add(self.mug, 1, session="")
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(self.call("get", "fragment", session="").json()["count"], 0)
        self.assertEqual(Cart.objects.count(), before)

    def test_output_is_escaped_and_unsafe_links_never_reach_the_page(self):
        from . import cart as cart_data

        evil = self.ok("post", "/api/v1/catalog/products/", {
            "title": '<script>alert(1)</script> "x"', "sku": "EVIL", "status": "active", "price": "1",
            "location": self.location, "quantity": 2,
        }, expect=201)
        self.add(evil, 1)
        html = self.call("get", "fragment").json()["html"]
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        for unsafe in ("javascript:alert(1)", "data:text/html,x", "//evil.example/x", " vbscript:x"):
            self.assertEqual(cart_data._safe_url(unsafe), "", unsafe)
        for safe in ("/checkout", "https://pay.example.com/x", "mailto:a@b.co", "#top"):
            self.assertEqual(cart_data._safe_url(safe), safe, safe)

        # the owner cannot save a javascript: link as the checkout button's address
        header = self.ok("get", "/api/v1/themes/sections/")["header"]
        values = header["values"]
        cart = next(b for b in values["blocks"] if b["type"] == "cart")
        cart["settings"]["checkout_url"] = "javascript:alert(1)"
        r = self.client.patch("/api/v1/themes/settings/", {"sections": {"header": values}}, format="json")
        self.assertEqual(r.status_code, 400, r.content[:300])

    def test_checkout_button_follows_the_owner_setting(self):
        self.add(self.mug, 1)
        html = self.call("get", "fragment").json()["html"]
        self.assertIn("data-cart-checkout-soon", html)  # no address set: "Checkout is coming soon"
        header = self.ok("get", "/api/v1/themes/sections/")["header"]
        values = header["values"]
        cart = next(b for b in values["blocks"] if b["type"] == "cart")
        cart["settings"].update(checkout_url="/checkout", checkout_label="Pay now", show_discount=False)
        self.ok("patch", "/api/v1/themes/settings/", {"sections": {"header": values}, "client": "test"})
        html = self.call("get", "fragment").json()["html"]
        self.assertIn(f'href="/s/{self.slug}/checkout">Pay now</a>', html)
        self.assertNotIn("cart-summary__muted", html)  # the discount row is off

    def test_the_cookie_is_only_given_to_pages_that_have_the_cart_and_is_scoped_to_the_store(self):
        from rest_framework.test import APIClient

        fresh = APIClient()
        response = fresh.get(f"/s/{self.slug}/")
        cookie = response.cookies[f"tss_cart_{self.slug}"]
        self.assertEqual(cookie["path"], f"/s/{self.slug}/")
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertEqual(len(cookie.value), 32)
        again = fresh.get(f"/s/{self.slug}/")  # the cookie is sent back: it is not replaced
        self.assertNotIn(f"tss_cart_{self.slug}", again.cookies)
        self.assertIn(f'data-cart-session="{cookie.value}"', again.content.decode())

        # no cart on the page (the owner removed the icon): no cookie is set
        header = self.ok("get", "/api/v1/themes/sections/")["header"]
        values = header["values"]
        values["blocks"] = [b for b in values["blocks"] if b["type"] != "cart"]
        self.ok("patch", "/api/v1/themes/settings/", {"sections": {"header": values}, "client": "test"})
        # the product cards' cart icon (a "Cart icon" block in each product section) needs the cart too: hide it
        index = self.ok("get", "/api/v1/themes/settings/")["templates"]["index"]
        for section in index["sections"].values():
            for block in section.get("blocks", []):
                if block["type"] == "cart_icon":
                    block["hidden"] = True
        self.ok("patch", "/api/v1/themes/settings/", {"templates": {"index": {"order": index["order"], "sections": index["sections"]}}})
        self.assertNotIn(f"tss_cart_{self.slug}", APIClient().get(f"/s/{self.slug}/").cookies)

    def test_the_endpoints_answer_cors_for_the_sandboxed_page(self):
        from rest_framework.test import APIClient

        preflight = APIClient().options(f"/s/{self.slug}/cart/add", HTTP_ORIGIN="null",
                                         HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
                                         HTTP_ACCESS_CONTROL_REQUEST_HEADERS="x-cart-session,content-type")
        self.assertEqual(preflight.status_code, 204)
        self.assertEqual(preflight["Access-Control-Allow-Origin"], "*")
        self.assertIn("X-Cart-Session", preflight["Access-Control-Allow-Headers"])
        reply = self.add(self.mug, 1)
        self.assertEqual(reply["Access-Control-Allow-Origin"], "*")
        self.assertEqual(reply["Cache-Control"], "no-store")
        self.assertEqual(self.call("get", "fragment", slug="no-such-store").status_code, 404)

    def test_the_cart_page_finds_the_cart_from_the_link_when_the_browser_kept_no_cookie(self):
        """The script puts the cart id in the cart link; a browser without the cookie still sees its cart."""
        import re

        from rest_framework.test import APIClient

        self.add(self.mug, 2)  # the shopper's page used self.session
        page = self.shopper.get(f"/s/{self.slug}/products/{self.mug['id']}").content.decode()
        self.assertIn(f'data-cart-session="{self.session}"', page)

        lost = APIClient()  # no cookie: the browser did not keep it
        response = lost.get(f"/s/{self.slug}/cart?cart={self.session}")
        html = response.content.decode()
        self.assertIn("Clay mug", html)  # the product is on the cart page
        self.assertNotIn("Your cart is empty", html)
        self.assertEqual(self.count_in(html[html.index("<header"):html.index("</header>")]), 2)  # and in the header
        self.assertEqual(response.cookies[f"tss_cart_{self.slug}"].value, self.session)  # the cookie is given back
        self.assertIn("Clay mug", lost.get(f"/s/{self.slug}/cart").content.decode())  # the next page needs no link

        # a made-up or foreign id shows an empty cart and a fresh id, never someone else's cart
        for bad in ("zzz", "A" * 32, "0" * 32):
            empty = APIClient().get(f"/s/{self.slug}/cart?cart={bad}")
            self.assertIn("Your cart is empty", empty.content.decode(), bad)
        other_cart_other_store = APIClient().get(f"/s/{self.slug}/cart?cart={'1' * 32}")
        self.assertNotIn("Clay mug", other_cart_other_store.content.decode())

        # when the browser has a cookie, the cookie wins: a link cannot swap someone's cart for another
        stranger = APIClient()
        mine = self.page_session(stranger, self.slug)
        page = stranger.get(f"/s/{self.slug}/cart?cart={self.session}").content.decode()
        self.assertNotIn("Clay mug", page)
        self.assertIn(f'data-cart-session="{mine}"', page)
        self.assertEqual(re.search(r'data-cart-session="([0-9a-f]{32})"', page).group(1), mine)

    def test_the_cart_link_carries_the_id_through_the_script_not_the_server_markup(self):
        """The links are tagged by the script (the server's page has the plain /cart link)."""
        header = self.header()
        self.assertIn(f'href="/s/{self.slug}/cart"', header)
        self.assertNotIn("?cart=", header)

    def test_the_cart_script_tags_the_cart_links(self):
        from .services import starter_files

        snippet = starter_files()["snippets/cart.liquid"]
        self.assertIn('document.querySelectorAll("a.site-header__cart")', snippet)
        self.assertIn('"cart=" + id', snippet)

    def test_the_product_page_adds_to_the_cart_instead_of_saying_coming_soon(self):
        page = self.shopper.get(f"/s/{self.slug}/products/{self.mug['id']}").content.decode()
        self.assertNotIn("Cart and checkout are coming soon", page)
        self.assertIn("data-cart-add", page)
        self.assertIn(f'name="product" value="{self.mug["id"]}"', page)
        self.assertIn(f'data-cart-base="/s/{self.slug}/cart"', page)
        self.assertIn(f'data-cart-session="{self.session}"', page)
        # a sold-out product's button is disabled, and the server refuses it anyway
        gone = self.shopper.get(f"/s/{self.slug}/products/{self.gone['id']}").content.decode()
        self.assertRegex(gone, r"data-add[^>]*\sdisabled>\s*Sold out")


class CartUpgradeTest(StoreClientMixin, APITestCase):
    """Existing stores get the cart snippet and the header block safely, and keep what they have."""

    SNIPPET = "snippets/cart.liquid"

    def setUp(self):
        from apps.tenancy.models import Store

        self.token, self.store = self.sign_up("cartupgrade@example.com", "Cart Upgrade Store")
        self.record = Store.objects.get(store_id=self.store["storeId"])
        self.base = f"/s/{self.store['storeSlug']}"

    def theme(self):
        from .services import get_store_theme

        return get_store_theme(self.record)

    def stored(self, path):
        from .models import Theme

        return Theme.objects.get(slug=f"store-{self.store['storeId']}").files.get(file_path=path).content

    def revert_to_revision_20(self, header=None, product=None, delete_snippet=True):
        """The store as it was before the cart: the older starter files, no cart snippet."""
        theme = self.theme().theme
        for path, content in (("sections/header.liquid", header or wishlist_era_header()),
                              ("sections/main-product.liquid", product or wishlist_era_main_product())):
            theme.files.filter(file_path=path).update(content=content, file_size=len(content.encode("utf-8")))
        if delete_snippet:
            theme.files.filter(file_path=self.SNIPPET).delete()
        data = {k: v for k, v in theme.theme_data.items() if k not in ("cart_icon_offered",)}
        theme.theme_data = {**data, "starter_revision": 20}
        theme.save()

    def test_a_new_store_has_one_cart_file_and_it_is_in_the_file_tree(self):
        files = [f["path"] for f in self.ok("get", "/api/v1/themes/files/")["files"]]
        self.assertEqual([p for p in files if "cart" in p and p.startswith("snippets/")], [self.SNIPPET])
        self.assertEqual([p for p in files if p.startswith("templates/") and "cart" in p], [])  # the page needs no template
        self.assertIn("{% render 'cart', mode: 'icon'", self.stored("sections/header.liquid"))
        self.assertIn("{% render 'cart', mode: 'script' %}", self.stored("sections/main-product.liquid"))

    def test_an_existing_store_gets_the_snippet_and_the_new_starter_files(self):
        import hashlib

        self.revert_to_revision_20()
        self.assertFalse(self.theme().theme.files.filter(file_path="snippets/other").exists())
        theme = self.theme().theme
        self.assertTrue(theme.files.filter(file_path=self.SNIPPET).exists())
        self.assertEqual(theme.theme_data["starter_revision"], 36)
        self.assertIn("{% render 'cart', mode: 'icon'", self.stored("sections/header.liquid"))
        self.assertIn("{% render 'cart', mode: 'script' %}", self.stored("sections/main-product.liquid"))
        self.assertIsNotNone(hashlib)

    def old_cart_snippet(self):
        """The cart snippet as the first revision shipped it (before the script kept a header's own badge up to date)."""
        current = self.snippet_before_link_ids()
        start = current.index("    // A header that draws its own cart icon")
        end = current.index("    });\n", start) + len("    });\n")
        return current[:start] + current[end:]

    def snippet_before_link_ids(self):
        """The snippet as revision 22 shipped it: the script kept a header's own badge, but links had no cart id."""
        from .services import starter_files

        current = starter_files()[self.SNIPPET]
        start = current.index("\n  // The cart link carries the cart id")
        end = current.index("  tagLinks();\n", start) + len("  tagLinks();\n")
        older = current[:start] + current[end:]
        click = '    if (target.closest("a.site-header__cart")) tagLinks();\n\n'
        assert click in older
        older = older.replace(click, "", 1)
        # the product card's cart icon came after: the revision-23 file did not have its click handler
        quick = older.index("\n    // The cart icon on a product card")
        quick_end = older.index("      return;\n    }\n", quick) + len("      return;\n    }\n")
        return older[:quick] + older[quick_end:]

    def test_the_snippet_from_the_previous_revision_is_registered_for_the_upgrade(self):
        import hashlib

        from .services import STARTER_UPGRADES, starter_files

        text = self.snippet_before_link_ids()
        self.assertNotIn("tagLinks", text)
        self.assertIn("A header that draws its own cart icon", text)  # still the revision-22 file, with the badge code
        self.assertIn(hashlib.sha256(text.encode("utf-8")).hexdigest(), STARTER_UPGRADES[self.SNIPPET])
        # a store with that copy gets the new one, an edited copy stays as it is
        theme = self.theme().theme
        theme.files.filter(file_path=self.SNIPPET).update(content=text, file_size=len(text.encode("utf-8")))
        theme.theme_data = {**theme.theme_data, "starter_revision": 22}
        theme.save()
        self.theme()
        self.assertEqual(self.stored(self.SNIPPET), starter_files()[self.SNIPPET])

    def test_an_untouched_older_cart_snippet_is_upgraded_and_an_edited_one_is_not(self):
        import hashlib

        from .services import STARTER_UPGRADES, starter_files

        old = self.old_cart_snippet()
        self.assertIn(hashlib.sha256(old.encode("utf-8")).hexdigest(), STARTER_UPGRADES[self.SNIPPET])  # a real older file
        theme = self.theme().theme
        theme.files.filter(file_path=self.SNIPPET).update(content=old, file_size=len(old.encode("utf-8")))
        theme.theme_data = {**theme.theme_data, "starter_revision": 21}
        theme.save()
        self.theme()
        self.assertEqual(self.stored(self.SNIPPET), starter_files()[self.SNIPPET])  # untouched: replaced

        mine = old + "<!-- mine -->\n"
        theme.files.filter(file_path=self.SNIPPET).update(content=mine, file_size=len(mine))
        theme.theme_data = {**theme.theme_data, "starter_revision": 21}
        theme.save()
        self.theme()
        self.assertEqual(self.stored(self.SNIPPET), mine)  # edited: kept

    def test_an_owner_edited_cart_snippet_is_not_overwritten(self):
        self.revert_to_revision_20(delete_snippet=False)
        mine = "{%- comment -%} my own cart {%- endcomment -%}\n"
        theme = self.theme().theme
        theme.files.filter(file_path=self.SNIPPET).update(content=mine, file_size=len(mine))
        self.theme()
        self.assertEqual(self.stored(self.SNIPPET), mine)

    def test_an_existing_header_with_saved_values_gets_the_block_type_but_no_new_icon(self):
        """Nothing appears on a live store by itself: the owner adds the cart icon (it is in Add block)."""
        self.revert_to_revision_20()
        store_theme = self.theme()
        store_theme.custom_data = {"sections": {"header": {
            "settings": {"logo_text": "Old shop"},
            "blocks": [{"type": "link", "settings": {"label": "About", "url": "/pages/about"}}],
        }}}
        store_theme.save()
        header = self.ok("get", "/api/v1/themes/sections/")["header"]
        self.assertIn("cart", header["spec"]["blocks"])  # offered
        self.assertEqual([b["type"] for b in header["values"]["blocks"]], ["link"])  # not added
        html = self.client.get(f"{self.base}/").content.decode()
        self.assertNotIn('class="site-header__cart"', html[html.index("<header"):html.index("</header>")])

    def test_an_owner_edited_header_gets_the_cart_block_by_insertion_only(self):
        from . import section_sync as sync

        header = edited_header()
        self.revert_to_revision_20(header=header)
        store_theme = self.theme()
        patched = self.stored("sections/header.liquid")
        self.assertNotEqual(patched, header)
        self.assertIn("owner note: keep this line", patched)  # the owner's edits stay
        self.assertIn("site-header__account", patched)
        self.assertIn("cart", [b["type"] for b in sync.parse(patched)["blocks"]])
        self.assertIn("profile", [b["type"] for b in sync.parse(patched)["blocks"]])
        # only insertions: taking them out gives the owner's header back, character for character
        undone = patched.replace(sync.CART_BLOCK_MARKUP, "").replace(",\n    " + sync.CART_BLOCK_SCHEMA, "")
        self.assertEqual(undone, header)
        self.assertTrue(sync.history_of(store_theme.theme, "header"))  # the old code can be restored
        self.assertIn("cart", self.ok("get", "/api/v1/themes/sections/")["header"]["spec"]["blocks"])
        # once: an owner who removes it from the code does not get it back
        removed = undone
        self.ok("post", "/api/v1/themes/files/", {"path": "sections/header.liquid", "content": removed})
        self.theme()
        self.assertEqual(self.stored("sections/header.liquid"), removed)

    def test_the_patcher_is_insert_only_idempotent_and_keeps_crlf(self):
        from . import section_sync as sync

        header = edited_header()
        patched = sync.add_cart_block(header)
        self.assertIsNotNone(patched)
        sync.parse(patched, "sections/header.liquid")
        self.assertIsNone(sync.add_cart_block(patched))  # done once
        # a header that already has the wishlist icon gets both blocks side by side
        both = sync.add_cart_block(sync.add_wishlist_block(header))
        self.assertEqual({b["type"] for b in sync.parse(both)["blocks"]}, {"link", "profile", "wishlist", "cart"})
        crlf = sync.add_cart_block(header.replace("\n", "\r\n"))
        self.assertIsNotNone(crlf)
        self.assertNotIn("\r\r\n", crlf)
        self.assertIsNone(sync.add_cart_block(header.replace("</header>", "")))  # nowhere safe to insert
        self.assertIsNone(sync.add_cart_block(header.replace('"blocks": [', '"things": [', 1)))

    def test_a_header_that_cannot_be_patched_is_left_alone_and_not_retried(self):
        broken = edited_header().replace("</header>", "")
        self.revert_to_revision_20(header=broken)
        self.theme()
        self.assertEqual(self.stored("sections/header.liquid"), broken)
        self.assertEqual(self.theme().theme.theme_data["cart_icon_offered"], 1)


class StorefrontWishlistTest(StoreClientMixin, APITestCase):
    def setUp(self):
        self.token, self.store = self.sign_up("wishlist@example.com", "Wishlist Store")
        self.product = self.ok("post", "/api/v1/catalog/products/", {
            "title": "Blue mug", "sku": "SKU-MUG", "status": "active", "price": "12.50",
        }, expect=201)
        self.base = f"/s/{self.store['storeSlug']}"

    def show_hearts(self):
        """What an owner does to get hearts: the "Wishlist heart" block on every card section, and on the product page."""
        data = self.ok("get", "/api/v1/themes/settings/")
        for page in ("index", "collection", "product"):
            sections = data["templates"][page]["sections"]
            for section in sections.values():
                if section["type"] in ("product-grid", "product-carousel", "main-collection") \
                        and not any(b["type"] == "wishlist_heart" for b in section["blocks"]):
                    section["blocks"].append({"type": "wishlist_heart", "hidden": False, "settings": {}})
                if section["type"] == "main-product" and not any(b["type"] == "wishlist_heart" for b in section["blocks"]):
                    section["blocks"].append({"type": "wishlist_heart", "settings": {}})
            self.ok("patch", "/api/v1/themes/settings/", {"templates": {page: {
                "order": data["templates"][page]["order"], "sections": sections}}})

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

        self.show_hearts()

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

    def test_the_theme_has_exactly_one_wishlist_file(self):
        """snippets/wishlist.liquid is the only wishlist file; the page needs no template."""
        from .services import REQUIRED_FILES, starter_files

        self.assertEqual([p for p in starter_files() if "wishlist" in p], ["snippets/wishlist.liquid"])
        self.assertEqual([p for p in REQUIRED_FILES if "wishlist" in p], ["snippets/wishlist.liquid"])
        files = self.ok("get", "/api/v1/themes/files/")["files"]
        self.assertEqual([f["path"] for f in files if "wishlist" in f["path"]], ["snippets/wishlist.liquid"])
        self.assertEqual(self.client.get(f"{self.base}/wishlist/").status_code, 200)

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

    def make_store(self, email, name, quantity=None):
        """A second store: its own admin, its own products. Returns (slug, product)."""
        token, store = self.sign_up(email, name)
        location = self.ok("post", "/api/v1/inventory/locations/", {"name": "Main"}, expect=201)
        body = {"title": f"{name} mug", "sku": f"SKU-{name[:3].upper()}", "status": "active", "price": "5"}
        if quantity is not None:
            body.update(location=location["id"], quantity=quantity)
        product = self.ok("post", "/api/v1/catalog/products/", body, expect=201)
        return store["storeSlug"], product

    def post(self, slug, action, product_id, json=True):
        headers = {"HTTP_ACCEPT": "application/json"} if json else {}
        return self.client.post(f"/s/{slug}/wishlist/items/", {"action": action, "product_id": product_id}, **headers)

    def test_click_a_heart_then_remove_it(self):
        """Save a product: the count goes up and /wishlist shows it with Add to cart. Remove: it goes and the count drops."""
        slug = self.store["storeSlug"]
        self.assertEqual(self.post(slug, "add", self.product["id"]).json()["count"], 1)
        page = self.client.get(f"/s/{slug}/wishlist/").content.decode()
        header = page[page.index("<header"):page.index("</header>")]
        self.assertIn("Blue mug", page)
        self.assertIn('data-wishlist-count aria-live="polite">1</span>', header)
        self.assertIn('class="button product-card__cta">Add to cart</span>', page)  # the saved list has Add to cart
        self.assertIn('data-wishlist-remove', page)

        self.assertEqual(self.post(slug, "remove", self.product["id"]).json()["count"], 0)
        page = self.client.get(f"/s/{slug}/wishlist/").content.decode()
        self.assertNotIn("Blue mug", page)
        self.assertIn("Your wishlist is empty.", page)
        self.assertIn('data-wishlist-count aria-live="polite">0</span>', page[page.index("<header"):page.index("</header>")])

    def test_the_sandboxed_page_saves_and_shows_the_wishlist_without_any_cookie(self):
        """The storefront page runs in a sandbox: its heart sends the cart id it was given, not the session cookie."""
        import re

        from rest_framework.test import APIClient

        slug = self.store["storeSlug"]
        home = APIClient().get(f"/s/{slug}/").content.decode()
        cart = re.search(r'data-cart-session="([0-9a-f]{32})"', home).group(1)
        self.assertNotIn("?cart=", home[home.index("<header"):home.index("</header>")])  # the id is added by the script

        # a click from the sandboxed page: no cookies at all, only the id from the page
        saved = APIClient().post(f"/s/{slug}/wishlist/items/", {
            "action": "add", "product_id": self.product["id"], "cart": cart}, HTTP_ACCEPT="application/json")
        self.assertEqual(saved.json()["count"], 1)
        self.assertEqual(saved["Access-Control-Allow-Origin"], "*")  # the page is a foreign origin

        # the navbar link, followed with no session cookie: the saved product is there
        page = APIClient().get(f"/s/{slug}/wishlist/?cart={cart}").content.decode()
        self.assertIn("Blue mug", page)
        self.assertIn('data-wishlist-count aria-live="polite">1</span>', page[page.index("<header"):page.index("</header>")])
        # another shopper sees nothing
        self.assertNotIn("Blue mug", APIClient().get(f"/s/{slug}/wishlist/").content.decode())
        # a heart posted as a plain form (no script) comes back to the page with the shopper's id
        plain = APIClient().post(f"/s/{slug}/wishlist/items/", {
            "action": "add", "product_id": self.product["id"], "cart": cart, "return_to": f"/s/{slug}/collections/all"})
        self.assertEqual(plain["Location"], f"/s/{slug}/collections/all?cart={cart}")
        from .services import starter_files

        self.assertIn('fetch(form.getAttribute("action")', starter_files()["snippets/wishlist.liquid"])
        # and a click can remove it again
        gone = APIClient().post(f"/s/{slug}/wishlist/items/", {
            "action": "remove", "product_id": self.product["id"], "cart": cart}, HTTP_ACCEPT="application/json")
        self.assertEqual(gone.json()["count"], 0)
        self.assertNotIn("Blue mug", APIClient().get(f"/s/{slug}/wishlist/?cart={cart}").content.decode())

    def test_a_sold_out_product_shows_sold_out_in_the_wishlist(self):
        slug = self.store["storeSlug"]
        location = self.ok("post", "/api/v1/inventory/locations/", {"name": "Main"}, expect=201)
        gone = self.ok("post", "/api/v1/catalog/products/", {
            "title": "Gone mug", "sku": "SKU-GONE", "status": "active", "price": "7",
            "location": location["id"], "quantity": 0,
        }, expect=201)
        fresh = self.ok("post", "/api/v1/catalog/products/", {
            "title": "Fresh mug", "sku": "SKU-FRESH", "status": "active", "price": "7",
            "location": location["id"], "quantity": 3,
        }, expect=201)
        self.post(slug, "add", gone["id"])
        self.post(slug, "add", fresh["id"])
        page = self.client.get(f"/s/{slug}/wishlist/").content.decode()
        card = page[page.index("Gone mug") - 700:page.index("Gone mug") + 400]
        self.assertIn("product-card--sold-out", card)
        self.assertIn('product-card__badge">Sold out</span>', card)
        self.assertEqual(page.count('product-card__badge">Sold out</span>'), 1)  # the mug in stock is not marked

    def test_another_shopper_and_another_store_cannot_see_or_change_these_items(self):
        slug = self.store["storeSlug"]
        # (signing up the second store starts a new login session, so it is done before anything is saved)
        other_slug, other_product = self.make_store("wishlist2@example.com", "Other Store")
        self.post(slug, "add", self.product["id"])
        # another shopper (a new session) has an empty wishlist, in the same store
        from rest_framework.test import APIClient

        stranger = APIClient()
        page = stranger.get(f"/s/{slug}/wishlist/").content.decode()
        self.assertIn("Your wishlist is empty.", page)
        self.assertNotIn("Blue mug", page)

        # another store: its page never shows this store's items, and ids don't cross stores
        self.assertEqual(self.post(other_slug, "add", self.product["id"]).status_code, 404)  # not a product of that store
        self.assertEqual(self.post(slug, "add", other_product["id"]).status_code, 404)
        self.assertEqual(self.post(other_slug, "add", other_product["id"]).json()["count"], 1)
        other_page = self.client.get(f"/s/{other_slug}/wishlist/").content.decode()
        self.assertIn("Other Store mug", other_page)
        self.assertNotIn("Blue mug", other_page)
        # this shopper's list in the first store is untouched
        self.assertIn("Blue mug", self.client.get(f"/s/{slug}/wishlist/").content.decode())
        self.assertNotIn("Other Store mug", self.client.get(f"/s/{slug}/wishlist/").content.decode())

    def test_output_is_escaped(self):
        slug = self.store["storeSlug"]
        evil = self.ok("post", "/api/v1/catalog/products/", {
            "title": '<script>alert(1)</script> "x"', "sku": "SKU-EVIL", "status": "active", "price": "1",
        }, expect=201)
        self.post(slug, "add", evil["id"])
        page = self.client.get(f"/s/{slug}/wishlist/").content.decode()
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        # a return address outside the store is ignored
        r = self.client.post(f"/s/{slug}/wishlist/items/", {"action": "add", "product_id": self.product["id"],
                                                             "return_to": "javascript:alert(1)"})
        self.assertEqual(r["Location"], f"/s/{slug}/wishlist/")

    def test_a_new_store_shows_no_hearts_but_the_wishlist_page_keeps_its_remove_button(self):
        """Hearts are off by default: not on cards, not on the product page. The wishlist page still removes items."""
        slug = self.store["storeSlug"]
        for path in ("", "collections/all", f"products/{self.product['id']}"):
            html = self.client.get(f"/s/{slug}/{path}").content.decode()
            self.assertNotIn('class="product-card__wishlist-form"', html, path)
            self.assertNotIn('class="product__wishlist-form"', html, path)
        # ...yet the header icon is there, and a saved product can be removed on the wishlist page
        self.client.post(f"/s/{slug}/wishlist/items/", {"action": "add", "product_id": self.product["id"]})
        page = self.client.get(f"/s/{slug}/wishlist/").content.decode()
        self.assertIn("Blue mug", page)
        self.assertIn('class="product-card__wishlist-remove"', page)
        self.assertIn("data-wishlist-remove", page)
        self.assertIn('aria-label="Wishlist"', page[page.index("<header"):page.index("</header>")])

    def test_adding_the_heart_block_shows_the_hearts_and_writes_the_block_into_the_code(self):
        data = self.ok("get", "/api/v1/themes/settings/")
        main = data["templates"]["collection"]["sections"]["main"]
        self.assertNotIn("wishlist_heart", [b["type"] for b in main["blocks"]])  # nothing adds it
        main["blocks"].append({"type": "wishlist_heart", "hidden": False, "settings": {}})
        self.ok("patch", "/api/v1/themes/settings/", {"templates": {"collection": {
            "order": data["templates"]["collection"]["order"], "sections": {"main": main}}}})
        files = {f["path"]: f["content"] for f in self.ok("get", "/api/v1/themes/files/")["files"]}
        self.assertIn("wishlist_heart", [b["type"] for b in json.loads(files["templates/collection.json"])["sections"]["main"]["blocks"]])
        html = self.client.get(f"{self.base}/collections/all").content.decode()
        self.assertIn('class="product-card__wishlist-form"', html)

    def test_add_from_product_page_and_invalid_redirect(self):
        self.show_hearts()
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


class WishlistHeartBlockTest(StoreClientMixin, APITestCase):
    """The product page's "Wishlist heart" block, in the customizer (blocks) and in Edit code (templates/product.json)."""

    PRODUCT_JSON = "templates/product.json"

    def setUp(self):
        self.token, self.store = self.sign_up("heartblock@example.com", "Heart Store")
        self.product = self.ok("post", "/api/v1/catalog/products/", {
            "title": "Clay mug", "sku": "SKU-MUG", "status": "active", "price": "12",
        }, expect=201)

    def page(self):
        r = self.client.get(f"/s/{self.store['storeSlug']}/products/{self.product['id']}")
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def main(self):
        return self.ok("get", "/api/v1/themes/settings/")["templates"]["product"]["sections"]["main"]

    def save_customizer(self, change):
        data = self.ok("get", "/api/v1/themes/settings/")
        main = data["templates"]["product"]["sections"]["main"]
        change(main)
        self.ok("patch", "/api/v1/themes/settings/", {"templates": {"product": {
            "order": data["templates"]["product"]["order"], "sections": {"main": main}}}})

    def code(self):
        files = {f["path"]: f["content"] for f in self.ok("get", "/api/v1/themes/files/")["files"]}
        return json.loads(files[self.PRODUCT_JSON])

    def save_code(self, change):
        product = self.code()
        change(product["sections"]["main"])
        r = self.client.post("/api/v1/themes/files/", {"path": self.PRODUCT_JSON, "content": json.dumps(product, indent=2)},
                             format="json")
        self.assertEqual(r.status_code, 200, r.content[:500])

    def hearts(self, html):
        return html.count('class="product__wishlist-form"')

    def types(self, blocks):
        return [b["type"] for b in blocks]

    def add_block(self, **settings):
        self.save_customizer(lambda m: m["blocks"].append({"type": "wishlist_heart", "settings": settings}))

    def test_heart_block_is_not_added_by_default_but_can_be_added(self):
        self.assertNotIn("wishlist_heart", self.types(self.main()["blocks"]))  # a new store: no heart on the product page
        self.assertEqual(self.hearts(self.page()), 0)
        spec = self.ok("get", "/api/v1/themes/schema/")["sections"]["main-product"]["blocks"]["wishlist_heart"]
        self.assertEqual(spec["name"], "Wishlist heart")  # it is in "Add block"
        self.assertEqual(set(spec["settings"]), {"label", "size", "align"})
        self.add_block()
        self.assertIn("wishlist_heart", self.types(self.code()["sections"]["main"]["blocks"]))
        html = self.page()
        self.assertEqual(self.hearts(html), 1)
        self.assertIn("Add to wishlist", html)

    def test_the_cart_icon_block_stays_in_step_between_the_customizer_and_the_code(self):
        carts = lambda html: html.count('class="site-header__cart"')
        base = carts(self.page())  # the header's own cart icon
        cart_blocks = lambda blocks: [b for b in blocks if b["type"] == "cart_icon"]
        self.assertEqual(cart_blocks(self.main()["blocks"]), [])  # nothing adds it
        self.assertEqual(cart_blocks(self.code()["sections"]["main"]["blocks"]), [])
        spec = self.ok("get", "/api/v1/themes/schema/")["sections"]["main-product"]["blocks"]["cart_icon"]
        self.assertEqual(spec["name"], "Cart icon")  # it is in "Add block"

        # Add block (customizer) -> code, store
        self.save_customizer(lambda m: m["blocks"].append({"type": "cart_icon", "settings": {}}))
        self.assertEqual(len(cart_blocks(self.code()["sections"]["main"]["blocks"])), 1)
        self.assertEqual(carts(self.page()), base + 1)

        # Edited in the code -> customizer, store
        def to_code(main):
            for block in cart_blocks(main["blocks"]):
                block["settings"].update(label="Basket", size=30, align="right", opens="page")
        self.save_code(to_code)
        block = cart_blocks(self.main()["blocks"])[0]
        self.assertEqual((block["settings"]["label"], block["settings"]["size"], block["settings"]["align"]), ("Basket", 30, "right"))
        html = self.page()
        self.assertEqual(carts(html), base + 1)
        self.assertIn('aria-label="Basket"', html)
        self.assertIn("width:30px", html)
        self.assertIn("text-align:right", html)

        # Hidden (customizer) -> code, store
        self.save_customizer(lambda m: cart_blocks(m["blocks"])[0].update(hidden=True))
        self.assertTrue(cart_blocks(self.code()["sections"]["main"]["blocks"])[0]["hidden"])
        self.assertEqual(carts(self.page()), base)
        self.save_code(lambda m: cart_blocks(m["blocks"])[0].update(hidden=False))  # shown again in the code
        self.assertFalse(cart_blocks(self.main()["blocks"])[0].get("hidden"))
        self.assertEqual(carts(self.page()), base + 1)

        # Deleted (code) -> customizer, store
        self.save_code(lambda m: m.update(blocks=[b for b in m["blocks"] if b["type"] != "cart_icon"]))
        self.assertEqual(cart_blocks(self.main()["blocks"]), [])
        self.assertEqual(carts(self.page()), base)

        # Added in the code -> customizer, store
        self.save_code(lambda m: m["blocks"].append({"type": "cart_icon", "settings": {"size": 28}}))
        self.assertEqual(len(cart_blocks(self.main()["blocks"])), 1)
        self.assertEqual(carts(self.page()), base + 1)

    def test_removing_and_adding_the_block_in_the_customizer(self):
        self.add_block()
        self.assertEqual(self.hearts(self.page()), 1)
        self.save_customizer(lambda m: m.update(blocks=[b for b in m["blocks"] if b["type"] != "wishlist_heart"]))
        self.assertNotIn("wishlist_heart", self.types(self.code()["sections"]["main"]["blocks"]))
        self.assertEqual(self.hearts(self.page()), 0)

        self.save_customizer(lambda m: m["blocks"].append(
            {"type": "wishlist_heart", "settings": {"label": "Save for later", "size": 32, "align": "center"}}))
        self.assertIn("wishlist_heart", self.types(self.code()["sections"]["main"]["blocks"]))
        html = self.page()
        self.assertEqual(self.hearts(html), 1)
        self.assertIn("Save for later", html)
        self.assertIn('style="width:32px;height:32px"', html)
        self.assertIn('style="text-align:center"', html)

    def test_editing_the_block_in_code_updates_the_customizer(self):
        self.add_block()

        def to_code(main):
            for block in main["blocks"]:
                if block["type"] == "wishlist_heart":
                    block["settings"].update(label="", size=24, align="right")
        self.save_code(to_code)
        block = next(b for b in self.main()["blocks"] if b["type"] == "wishlist_heart")
        self.assertEqual((block["settings"]["label"], block["settings"]["size"], block["settings"]["align"]), ("", 24, "right"))
        html = self.page()
        self.assertNotIn("<span data-wishlist-label>", html)  # an empty label shows only the heart
        self.assertIn('style="text-align:right"', html)

        self.save_code(lambda main: main.update(blocks=[b for b in main["blocks"] if b["type"] != "wishlist_heart"]))
        self.assertNotIn("wishlist_heart", self.types(self.main()["blocks"]))
        self.assertEqual(self.hearts(self.page()), 0)

    def test_the_block_calls_the_snippet_and_the_product_page_has_no_wishlist_markup(self):
        from .services import starter_files

        product = starter_files()["sections/main-product.liquid"]
        self.assertIn("{% render 'wishlist', mode: 'button'", product)
        self.assertNotIn("data-wishlist-toggle", product)


def wishlist_era_main_product():
    """The starter product section from before the cart: add to cart only said "coming soon"."""
    from .services import starter_files

    product = starter_files()["sections/main-product.liquid"]
    for new, old in (
        # the "Cart icon" block, added to the product page after the cart
        ("""      {%- comment -%} The "Cart icon" block: add, hide or move it in the customizer {%- endcomment -%}
      {%- if section.cart_icon -%}
        <div class="product__cart-icon" style="text-align:{{ section.cart_icon.align | default: 'left' }}">{% render 'cart', mode: 'icon', count: cart_count, session: cart_session, settings: section.cart_icon %}</div>
      {%- endif -%}
""", ""),
        ("""      <form class="product__form" data-product-form data-cart-add data-cart-base="{{ shop.url }}/cart" data-cart-session="{{ cart_session }}">
        <input type="hidden" name="product" value="{{ product.id }}">
""", """      <form class="product__form" data-product-form>
"""),
        ("""
    // Add to cart: the form is handled by snippets/cart.liquid
""", """
    // The cart (commerce app) has no API yet, so the button only explains that.
    root.querySelector("[data-product-form]").addEventListener("submit", function (event) {
      event.preventDefault();
      root.querySelector("[data-notice]").textContent = "Cart and checkout are coming soon.";
    });
"""),
        ("{% render 'cart', mode: 'script' %}\n", ""),
    ):
        assert new in product, new[:60]
        product = product.replace(new, old, 1)
    return product


def legacy_main_product():
    """The starter product section from before the heart became a block: the heart form was inline."""
    from .services import starter_files

    heart = "M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78Z"
    new = """      {%- comment -%} The "Wishlist heart" block: add, remove or move it in the customizer {%- endcomment -%}
      {%- if section.wishlist_heart -%}
        {% render 'wishlist', mode: 'button', product: product, wishlist_ids: wishlist_ids, return_url: return_url, placement: 'product', size: section.wishlist_heart.size, label_text: section.wishlist_heart.label, align: section.wishlist_heart.align, with_script: true %}
      {%- endif -%}
"""
    old = f"""      {{%- assign is_wishlisted = false -%}}
      {{%- if wishlist_ids contains product.id -%}}{{%- assign is_wishlisted = true -%}}{{%- endif -%}}
      <form class="product__wishlist-form" method="post" action="{{{{ shop.url }}}}/wishlist/items/">
        <input type="hidden" name="product_id" value="{{{{ product.id }}}}">
        <input type="hidden" name="action" value="{{% if is_wishlisted %}}remove{{% else %}}add{{% endif %}}" data-wishlist-action>
        <input type="hidden" name="return_to" value="{{{{ return_url }}}}">
        <button class="product__wishlist-add{{% if is_wishlisted %}} is-wishlisted{{% endif %}}" type="submit"
          data-wishlist-toggle data-product-id="{{{{ product.id }}}}" data-product-title="{{{{ product.title | escape }}}}" aria-pressed="{{% if is_wishlisted %}}true{{% else %}}false{{% endif %}}"
          aria-label="{{% if is_wishlisted %}}Remove {{{{ product.title }}}} from{{% else %}}Add {{{{ product.title }}}} to{{% endif %}} wishlist">
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="{heart}" />
          </svg>
          <span data-wishlist-label>{{% if is_wishlisted %}}Remove from wishlist{{% else %}}Add to wishlist{{% endif %}}</span>
        </button>
      </form>
"""
    product = wishlist_era_main_product()
    assert new in product
    return product.replace(new, old, 1)


class WishlistUpgradeTest(StoreClientMixin, APITestCase):
    """Existing stores receive the wishlist through the starter revision; edited files are never overwritten."""

    SNIPPET = "snippets/wishlist.liquid"

    def setUp(self):
        from apps.tenancy.models import Store

        self.token, self.store = self.sign_up("wishupgrade@example.com", "Upgrade Store")
        self.product = self.ok("post", "/api/v1/catalog/products/", {
            "title": "Clay mug", "sku": "SKU-MUG", "status": "active", "price": "12",
        }, expect=201)
        self.record = Store.objects.get(store_id=self.store["storeId"])
        self.base = f"/s/{self.store['storeSlug']}"

    def theme(self):
        from .services import get_store_theme

        return get_store_theme(self.record)

    def file(self, path):
        return self.theme().theme.files.filter(file_path=path).values_list("content", flat=True).first()

    def stored(self, path):
        """The file as it is stored right now, without visiting the theme (which would run the upgrade)."""
        from .models import Theme

        return Theme.objects.get(slug=f"store-{self.store['storeId']}").files.get(file_path=path).content

    def header_html(self):
        html = self.client.get(f"{self.base}/").content.decode()
        return html[html.index("<header"):html.index("</header>")]

    def product_page(self):
        return self.client.get(f"{self.base}/products/{self.product['id']}").content.decode()

    def make_store_from_before_the_wishlist_blocks(self, edited=()):
        """The store as it was at revision 18: the old starter files, the heart as a setting, no snippet."""
        store_theme = self.theme()
        theme = store_theme.theme
        old = {"sections/header.liquid": legacy_header(), "sections/main-product.liquid": legacy_main_product()}
        for path, content in old.items():
            content = content + "<!-- mine -->\n" if path in edited else content
            theme.files.filter(file_path=path).update(content=content, file_size=len(content.encode("utf-8")))
        theme.files.filter(file_path=self.SNIPPET).delete()
        data = {k: v for k, v in theme.theme_data.items() if k != "wishlist_blocks"}
        theme.theme_data = {**data, "starter_revision": 18}
        theme.save()
        store_theme.custom_data = {
            "sections": {"header": {
                "settings": {"show_wishlist": True, "logo_text": "Old shop"},
                "blocks": [{"type": "link", "settings": {"label": "About", "url": "/pages/about"}}],
            }},
            "templates": {"product": {"order": ["main"], "sections": {"main": {"type": "main-product", "settings": {}}}}},
        }
        store_theme.save()

    def test_the_fixtures_for_older_stores_are_the_real_older_starter_files(self):
        """The upgrade only replaces files whose hash is registered: these are the files stores had before the cart."""
        import hashlib

        from .services import STARTER_UPGRADES

        for path, text in (("sections/header.liquid", wishlist_era_header()),
                           ("sections/main-product.liquid", wishlist_era_main_product())):
            self.assertIn(hashlib.sha256(text.encode("utf-8")).hexdigest(), STARTER_UPGRADES[path], path)

    def test_new_store_has_the_snippet_and_it_is_in_the_file_tree(self):
        files = [f["path"] for f in self.ok("get", "/api/v1/themes/files/")["files"]]
        self.assertEqual([p for p in files if "wishlist" in p], [self.SNIPPET])
        self.assertIn("{% render 'wishlist', mode: 'icon'", self.file("sections/header.liquid"))

    def test_existing_store_gets_the_snippet_after_the_upgrade(self):
        self.make_store_from_before_the_wishlist_blocks()
        from .models import Theme
        self.assertFalse(Theme.objects.get(slug=f"store-{self.store['storeId']}").files.filter(file_path=self.SNIPPET).exists())
        files = [f["path"] for f in self.ok("get", "/api/v1/themes/files/")["files"]]
        self.assertEqual([p for p in files if "wishlist" in p], [self.SNIPPET])
        self.assertIn("mode == 'page'", self.file(self.SNIPPET))

    def test_an_owner_edited_snippet_is_not_overwritten(self):
        mine = "{%- comment -%} my own wishlist {%- endcomment -%}\n"
        self.make_store_from_before_the_wishlist_blocks()  # the snippet is missing, as in an older store
        self.theme().theme.files.filter(file_path=self.SNIPPET).update(content=mine, file_size=len(mine))
        self.assertEqual(self.file(self.SNIPPET), mine)
        self.theme()  # every later visit to the theme
        self.assertEqual(self.file(self.SNIPPET), mine)

    def test_untouched_files_are_upgraded_and_the_hearts_stay_visible(self):
        import hashlib

        from . import section_sync as sync
        from .services import STARTER_UPGRADES

        self.make_store_from_before_the_wishlist_blocks()
        # the fixtures really are the old starter files (their hashes are registered for the upgrade)
        for path in ("sections/header.liquid", "sections/main-product.liquid"):
            digest = hashlib.sha256(self.stored(path).encode("utf-8")).hexdigest()
            self.assertIn(digest, STARTER_UPGRADES[path])

        store_theme = self.theme()
        self.assertEqual(store_theme.theme.theme_data["starter_revision"], 36)
        self.assertIn("{% render 'wishlist', mode: 'icon'", self.file("sections/header.liquid"))
        self.assertIn("section.wishlist_heart", self.file("sections/main-product.liquid"))
        self.assertIsNotNone(self.file(self.SNIPPET))
        # the replaced header is kept in its history (Restore)
        self.assertTrue(sync.history_of(store_theme.theme, "header"))

        # the header keeps the owner's link and gets the heart as a block; "show_wishlist" is gone
        header = self.ok("get", "/api/v1/themes/sections/")["header"]
        self.assertEqual([b["type"] for b in header["values"]["blocks"]], ["link", "wishlist"])
        self.assertEqual(header["values"]["settings"]["logo_text"], "Old shop")
        self.assertEqual([b["type"] for b in sync.parse(self.file("sections/header.liquid"))["default"]["blocks"]],
                         ["link", "wishlist"])
        self.assertIn('aria-label="Wishlist"', self.header_html())
        # the product page keeps its heart, now as a block
        product = self.ok("get", "/api/v1/themes/settings/")["templates"]["product"]["sections"]["main"]
        self.assertIn("wishlist_heart", [b["type"] for b in product["blocks"]])
        self.assertEqual(self.product_page().count('class="product__wishlist-form"'), 1)

        # running it again changes nothing
        version = self.ok("get", "/api/v1/themes/sections/")["header"]["version"]
        code = self.file("sections/header.liquid")
        self.theme()
        self.assertEqual(self.ok("get", "/api/v1/themes/sections/")["header"]["version"], version)
        self.assertEqual(self.file("sections/header.liquid"), code)

    def make_store_that_shows_hearts(self, saved_templates):
        """An existing store from before "Show wishlist heart" became opt-in: its theme was visited before, so it is not new."""
        store_theme = self.theme()
        theme = store_theme.theme
        theme.theme_data = {k: v for k, v in theme.theme_data.items() if k != "wishlist_hearts"}
        theme.save()
        store_theme.custom_data = {"templates": saved_templates} if saved_templates else {}
        store_theme.save()

    def hearts(self, path):
        html = self.client.get(f"{self.base}/{path}").content.decode()
        return html.count('class="product-card__wishlist-form"')

    def test_an_existing_store_keeps_its_hearts_after_the_upgrade(self):
        """Sections that never saved the setting showed the heart (it was on by default): they keep it."""
        self.make_store_that_shows_hearts({
            "collection": {"order": ["main"], "sections": {"main": {"type": "main-collection", "settings": {"columns": 3}}}},
        })  # the collection page was customized without the setting; the home and product pages never were
        self.assertEqual(self.theme().theme.theme_data["wishlist_hearts"], 1)
        settings = self.ok("get", "/api/v1/themes/settings/")["templates"]
        types = lambda section: [b["type"] for b in section["blocks"]]
        self.assertIn("wishlist_heart", types(settings["collection"]["sections"]["main"]))  # the heart is a block now
        self.assertEqual(settings["collection"]["sections"]["main"]["settings"]["columns"], 3)  # the owner's choice is kept
        for section in settings["index"]["sections"].values():
            if section["type"] in ("product-grid", "product-carousel"):
                self.assertIn("wishlist_heart", types(section), section["type"])
        self.assertGreater(self.hearts("collections/all"), 0)
        self.assertGreater(self.hearts(""), 0)
        # what was saved is in the code (templates/*.json), so the owner can see and change it
        files = {f["path"]: f["content"] for f in self.ok("get", "/api/v1/themes/files/")["files"]}
        self.assertIn("wishlist_heart", [b["type"] for b in json.loads(files["templates/collection.json"])["sections"]["main"]["blocks"]])
        # the product page keeps its heart too: its default block is saved
        product = settings["product"]["sections"]["main"]
        self.assertIn("wishlist_heart", [b["type"] for b in product["blocks"]])
        self.assertEqual(self.product_page().count('class="product__wishlist-form"'), 1)

    def test_an_owners_own_choice_is_not_changed_by_the_upgrade(self):
        self.make_store_that_shows_hearts({
            "collection": {"order": ["main"], "sections": {"main": {"type": "main-collection", "settings": {"show_wishlist": False}}}},
            "index": {"order": ["grid"], "sections": {"grid": {"type": "product-grid", "settings": {"show_wishlist": True}}}},
        })
        settings = self.ok("get", "/api/v1/themes/settings/")["templates"]
        self.assertNotIn("wishlist_heart", [b["type"] for b in settings["collection"]["sections"]["main"]["blocks"]])  # off by the owner
        self.assertIn("wishlist_heart", [b["type"] for b in settings["index"]["sections"]["grid"]["blocks"]])
        self.assertEqual(self.hearts("collections/all"), 0)

    def test_the_upgrade_runs_once_and_a_new_store_is_left_off(self):
        self.make_store_that_shows_hearts(None)
        self.theme()
        # the owner turns the hearts off after the upgrade: later visits do not turn them back on
        data = self.ok("get", "/api/v1/themes/settings/")
        main = data["templates"]["collection"]["sections"]["main"]
        main["blocks"] = [b for b in main["blocks"] if b["type"] != "wishlist_heart"]  # the owner deletes the block
        self.ok("patch", "/api/v1/themes/settings/", {"templates": {"collection": {
            "order": data["templates"]["collection"]["order"], "sections": {"main": main}}}})
        self.theme()
        self.assertEqual(self.hearts("collections/all"), 0)
        # a store created now was never migrated: hearts off, nothing saved
        other = self.sign_up("freshstore@example.com", "Fresh Store")[1]
        self.ok("post", "/api/v1/catalog/products/", {"title": "Mug", "sku": "S-M", "status": "active", "price": "1"}, expect=201)
        html = self.client.get(f"/s/{other['storeSlug']}/collections/all").content.decode()
        self.assertNotIn('class="product-card__wishlist-form"', html)
        from .models import StoreTheme

        fresh = StoreTheme.objects.get(store__store_id=other["storeId"], is_active=True)
        self.assertEqual(fresh.theme.theme_data["wishlist_hearts"], 1)  # marked done without saving anything
        saved = fresh.custom_data.get("templates") or {}
        self.assertFalse({"index", "collection", "product"} & set(saved))  # the card pages were not saved
        self.assertNotIn("show_wishlist", json.dumps(saved))  # nothing was written into any section
        self.assertNotIn("wishlist_heart", [b["type"] for b in self.ok("get", "/api/v1/themes/settings/")["templates"]
                                            ["collection"]["sections"]["main"]["blocks"]])

    def test_an_owner_edited_card_snippet_keeps_working_and_the_wishlist_page_can_remove(self):
        mine = ("<!-- my card -->\n<div class=\"product-card-shell\"><a href=\"{{ product.url }}\">{{ product.title }}</a>"
                "<form class=\"product-card__wishlist-form\" data-wishlist-form method=\"post\" "
                "action=\"{{ shop.url }}/wishlist/items/\"><input type=\"hidden\" name=\"product_id\" value=\"{{ product.id }}\">"
                "<input type=\"hidden\" name=\"action\" value=\"remove\" data-wishlist-action>"
                "{% if wishlist_action == 'remove' %}<button class=\"product-card__wishlist-remove\" type=\"submit\" "
                "data-wishlist-remove>Remove</button>{% endif %}</form></div>\n")
        self.theme().theme.files.filter(file_path="snippets/product-card.liquid").update(content=mine, file_size=len(mine))
        self.assertEqual(self.file("snippets/product-card.liquid"), mine)  # the upgrade does not touch it
        self.client.post(f"{self.base}/wishlist/items/", {"action": "add", "product_id": self.product["id"]})
        page = self.client.get(f"{self.base}/wishlist/").content.decode()
        self.assertIn("<!-- my card -->", page)
        self.assertIn("Clay mug", page)
        self.assertIn('class="product-card__wishlist-remove"', page)  # still removable from the wishlist page

    def test_a_header_and_product_section_the_owner_edited_are_left_alone(self):
        self.make_store_from_before_the_wishlist_blocks(edited=("sections/header.liquid", "sections/main-product.liquid"))
        header, product = self.stored("sections/header.liquid"), self.stored("sections/main-product.liquid")
        store_theme = self.theme()
        self.assertEqual(self.file("sections/header.liquid"), header)
        self.assertEqual(self.file("sections/main-product.liquid"), product)
        values = self.ok("get", "/api/v1/themes/sections/")["header"]["values"]
        self.assertEqual([b["type"] for b in values["blocks"]], ["link"])  # no block the code can't show
        self.assertIsNone(store_theme.custom_data["templates"]["product"]["sections"]["main"].get("blocks"))
        # and the heart still shows, from the owner's own markup
        self.assertIn('aria-label="Wishlist"', self.header_html())
        self.assertEqual(self.product_page().count('class="product__wishlist-form"'), 1)


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

    def heading_texts(self, blocks):
        """The texts of the heading blocks (a product section also always has its "Cart icon" block)."""
        return [b["settings"]["text"] for b in blocks if b["type"] == "heading"]

    def test_the_wishlist_heart_is_a_block_the_owner_adds_hides_and_deletes_in_either_editor(self):
        """The heart starts absent. "Add block" adds it; the eye hides it; delete removes it. Both editors follow."""
        hearts = lambda html: html.count('class="product-card__wishlist-form"')
        heart_blocks = lambda blocks: [b for b in blocks if b["type"] == "wishlist_heart"]
        spec = self.ok("get", "/api/v1/themes/schema/")["sections"]["product-grid"]
        self.assertIn("wishlist_heart", spec["blocks"])  # it is in "Add block"
        self.assertNotIn("wishlist_heart", spec.get("required_blocks", []))  # and can be deleted
        self.assertEqual(heart_blocks(self.customizer_grid()["blocks"]), [])  # a new store: no hearts on cards
        self.assertEqual(hearts(self.preview_html()), 0)
        self.assertEqual(hearts(self.store_html()), 0)

        # Add block (customizer) -> code, preview and store; the heart is always visible
        self.save_customizer_grid(lambda s: s["blocks"].append({"type": "wishlist_heart", "hidden": False, "settings": {}}))
        self.assertEqual(len(heart_blocks(self.code_index()["sections"]["featured"]["blocks"])), 1)  # written to the code
        shown = hearts(self.store_html())
        self.assertGreater(shown, 0)
        self.assertEqual(hearts(self.preview_html()), shown)
        html = self.store_html()
        self.assertNotIn("data-wishlist-hover", html)  # the heart is always visible, not only on hover

        # Hidden in the customizer -> code, store; shown again in the code
        self.save_customizer_grid(lambda s: heart_blocks(s["blocks"])[0].update(hidden=True))
        self.assertTrue(heart_blocks(self.code_index()["sections"]["featured"]["blocks"])[0]["hidden"])
        self.assertEqual(hearts(self.store_html()), 0)
        self.save_code_index(lambda s: heart_blocks(s["blocks"])[0].update(hidden=False))
        self.assertFalse(heart_blocks(self.customizer_grid()["blocks"])[0].get("hidden"))
        self.assertEqual(hearts(self.store_html()), shown)

        # Deleted in the code -> customizer, store; added in the code -> customizer, store
        self.save_code_index(lambda s: s.update(blocks=[b for b in s["blocks"] if b["type"] != "wishlist_heart"]))
        self.assertEqual(heart_blocks(self.customizer_grid()["blocks"]), [])
        self.assertEqual(hearts(self.store_html()), 0)
        self.save_code_index(lambda s: s.setdefault("blocks", []).append({"type": "wishlist_heart", "settings": {"size": 28}}))
        self.assertEqual(heart_blocks(self.customizer_grid()["blocks"])[0]["settings"]["size"], 28)
        self.assertIn("width:28px", self.store_html())

        # the old "Show wishlist heart" setting is still understood: true is the block, false takes it away
        self.save_code_index(lambda s: s.update(blocks=[b for b in s["blocks"] if b["type"] != "wishlist_heart"]))
        self.save_code_index(lambda s: s["settings"].update(show_wishlist=True))
        self.assertEqual(len(heart_blocks(self.customizer_grid()["blocks"])), 1)
        self.save_code_index(lambda s: s["settings"].update(show_wishlist=False))
        self.assertEqual(heart_blocks(self.customizer_grid()["blocks"]), [])

    def test_the_wishlist_heart_and_the_cart_icon_blocks_work_independently(self):
        icons = lambda html: (html.count('class="product-card__wishlist-form"'), html.count('class="product-card__cart"'))
        location = self.ok("post", "/api/v1/inventory/locations/", {"name": "Main"}, expect=201)["id"]
        self.ok("post", "/api/v1/catalog/products/", {
            "title": "Stocked mug", "sku": "SKU-STOCK", "status": "active", "price": "12", "location": location, "quantity": 5,
        }, expect=201)
        self.save_customizer_grid(lambda s: s["blocks"].extend([
            {"type": "wishlist_heart", "hidden": False, "settings": {}}, {"type": "cart_icon", "hidden": False, "settings": {}}]))
        hearts, carts = icons(self.store_html())
        self.assertGreater(hearts, 0)
        self.assertGreater(carts, 0)
        # hiding the heart leaves the cart icon, and the other way round
        self.save_customizer_grid(lambda s: [b.update(hidden=True) for b in s["blocks"] if b["type"] == "wishlist_heart"])
        self.assertEqual(icons(self.store_html()), (0, carts))
        self.save_customizer_grid(lambda s: [b.update(hidden=False) for b in s["blocks"] if b["type"] == "wishlist_heart"])
        self.save_customizer_grid(lambda s: [b.update(hidden=True) for b in s["blocks"] if b["type"] == "cart_icon"])
        self.assertEqual(icons(self.store_html()), (hearts, 0))
        # deleting the cart icon block does not touch the heart
        self.save_customizer_grid(lambda s: s.update(blocks=[b for b in s["blocks"] if b["type"] != "cart_icon"]))
        self.assertEqual(icons(self.store_html())[0], hearts)

    def test_the_heart_markup_comes_from_the_wishlist_snippet(self):
        """Cards render the heart through {% render 'wishlist' %}: no wishlist markup of their own."""
        from .services import starter_files

        card = starter_files()["snippets/product-card.liquid"]
        self.assertIn("{% render 'wishlist', mode: 'button'", card)
        self.assertNotIn("data-wishlist-toggle", card)
        for section in ("product-grid", "product-carousel", "main-collection"):
            self.assertIn("show_wishlist: section.wishlist_heart", starter_files()[f"sections/{section}.liquid"])

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
        self.assertEqual(self.heading_texts(self.customizer_grid()["blocks"]), ["Block A", "Block B"])
        html = self.store_html()
        self.assertLess(html.index("Block A"), html.index("Block B"))
        # Reordered in the customizer
        self.save_customizer_grid(lambda s: s["blocks"].reverse())
        self.assertEqual(self.heading_texts(self.code_index()["sections"]["featured"]["blocks"]), ["Block B", "Block A"])
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
        self.assertEqual(self.heading_texts(self.customizer_grid()["blocks"]), ["Block A"])
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

    def test_8_cart_icon_block_stays_in_step_between_the_customizer_and_the_code(self):
        icons = lambda html: html.count('class="product-card__cart"')
        cart_blocks = lambda blocks: [b for b in blocks if b["type"] == "cart_icon"]
        location = self.ok("post", "/api/v1/inventory/locations/", {"name": "Main"}, expect=201)["id"]
        self.ok("post", "/api/v1/catalog/products/", {
            "title": "Stocked mug", "sku": "SKU-STOCK", "status": "active", "price": "12", "location": location, "quantity": 5,
        }, expect=201)

        # Added in the code -> customizer, preview, store
        self.save_code_index(lambda s: s.setdefault("blocks", []).append({"type": "cart_icon", "settings": {"size": 52}}))
        block = cart_blocks(self.customizer_grid()["blocks"])
        self.assertEqual(len(block), 1)
        self.assertEqual(block[0]["settings"]["size"], 52)
        shown = icons(self.store_html())
        self.assertGreater(shown, 0)
        self.assertIn("width:52px", self.store_html())
        self.assertEqual(icons(self.preview_html()), shown)

        # Changed in the customizer -> code, store
        self.save_customizer_grid(lambda s: cart_blocks(s["blocks"])[0]["settings"].update(size=40, label="Quick add"))
        saved = cart_blocks(self.code_index()["sections"]["featured"]["blocks"])[0]["settings"]
        self.assertEqual((saved["size"], saved["label"]), (40, "Quick add"))
        html = self.store_html()
        self.assertIn("width:40px", html)
        self.assertIn("Quick add", html)

        # Hidden in the customizer -> code, store; shown again in the code
        self.save_customizer_grid(lambda s: cart_blocks(s["blocks"])[0].update(hidden=True))
        self.assertTrue(cart_blocks(self.code_index()["sections"]["featured"]["blocks"])[0]["hidden"])
        self.assertEqual(icons(self.store_html()), 0)
        self.save_code_index(lambda s: cart_blocks(s["blocks"])[0].update(hidden=False))
        self.assertFalse(cart_blocks(self.customizer_grid()["blocks"])[0].get("hidden"))
        self.assertEqual(icons(self.store_html()), shown)

        # Deleted in the code -> customizer, store
        self.save_code_index(lambda s: s.update(blocks=[b for b in s["blocks"] if b["type"] != "cart_icon"]))
        self.assertEqual(cart_blocks(self.customizer_grid()["blocks"]), [])
        self.assertEqual(icons(self.store_html()), 0)

        # Added in the customizer -> code
        self.save_customizer_grid(lambda s: s["blocks"].append({"type": "cart_icon", "hidden": False, "settings": {}}))
        self.assertEqual(len(cart_blocks(self.code_index()["sections"]["featured"]["blocks"])), 1)
        self.assertEqual(icons(self.store_html()), shown)

    def test_9_cart_icon_block_and_the_cards_code_follow_each_other(self):
        CARD = "snippets/product-card.liquid"
        has_icon = lambda: "if show_cart_icon" in self.files()[CARD]
        cart_blocks = lambda blocks: [b for b in blocks if b["type"] == "cart_icon"]
        icons = lambda: self.store_html().count('class="product-card__cart"')
        location = self.ok("post", "/api/v1/inventory/locations/", {"name": "Main"}, expect=201)["id"]
        self.ok("post", "/api/v1/catalog/products/", {
            "title": "Stocked mug", "sku": "SKU-STOCK", "status": "active", "price": "12", "location": location, "quantity": 5,
        }, expect=201)
        original = self.files()[CARD]

        # a new store: no block and no icon in the card's code
        self.assertFalse(has_icon())
        self.assertEqual(icons(), 0)

        # Block added in the customizer -> the card's code gets the icon, the store shows it
        self.save_customizer_grid(lambda s: s["blocks"].append({"type": "cart_icon", "hidden": False, "settings": {}}))
        self.assertTrue(has_icon())
        self.assertIn("data-card-add", self.files()[CARD])
        self.assertGreater(icons(), 0)

        # Last block removed in the customizer -> the icon is taken out of the card's code (and nothing else)
        self.save_customizer_grid(lambda s: s["blocks"].clear())
        self.assertFalse(has_icon())
        self.assertEqual(self.files()[CARD], original)
        self.assertEqual(icons(), 0)

        # Icon written in the card's code -> a block appears in the product sections (customizer and templates/index.json), and in the store
        from .section_sync import add_card_cart_icon
        self.save_file(CARD, add_card_cart_icon(original))
        self.assertEqual(len(cart_blocks(self.customizer_grid()["blocks"])), 1)
        self.assertEqual(len(cart_blocks(self.code_index()["sections"]["featured"]["blocks"])), 1)
        self.assertGreater(icons(), 0)

        # Saving the card again with the icon where it was does not add another block
        self.save_file(CARD, self.files()[CARD] + "<!-- note -->")
        self.assertEqual(len(cart_blocks(self.customizer_grid()["blocks"])), 1)

        # A section that lost its block gets it back when the card (which has the icon) is saved again
        self.save_customizer_grid(lambda s: s.update(blocks=[b for b in s["blocks"] if b["type"] != "cart_icon"]))
        self.assertTrue(has_icon())  # the carousel still has its block, so the card keeps the icon
        self.save_file(CARD, self.files()[CARD] + "<!-- again -->")
        self.assertEqual(len(cart_blocks(self.customizer_grid()["blocks"])), 1)

        # Icon taken out of the card's code -> the blocks go, the store shows no icon
        self.save_file(CARD, original)
        self.assertEqual(cart_blocks(self.customizer_grid()["blocks"]), [])
        self.assertEqual(cart_blocks(self.code_index()["sections"]["featured"]["blocks"]), [])
        self.assertEqual(icons(), 0)

        # Block added in templates/index.json (Edit code) -> the card's code gets the icon
        self.save_code_index(lambda s: s.setdefault("blocks", []).append({"type": "cart_icon", "settings": {}}))
        self.assertTrue(has_icon())
        self.assertGreater(icons(), 0)

    def test_7_cart_icon_is_a_block_the_owner_adds_hides_and_deletes(self):
        icons = lambda html: html.count('class="product-card__cart"')
        spec = self.ok("get", "/api/v1/themes/schema/")["sections"]["product-grid"]
        self.assertIn("cart_icon", spec["blocks"])  # offered in "Add block"
        self.assertNotIn("cart_icon", spec.get("required_blocks", []))
        # a product in stock (a sold-out one never shows the icon)
        location = self.ok("post", "/api/v1/inventory/locations/", {"name": "Main"}, expect=201)["id"]
        self.ok("post", "/api/v1/catalog/products/", {
            "title": "Stocked mug", "sku": "SKU-STOCK", "status": "active", "price": "12", "location": location, "quantity": 5,
        }, expect=201)
        # nothing adds it
        self.assertEqual([b["type"] for b in self.customizer_grid()["blocks"]], [])
        self.assertEqual(icons(self.store_html()), 0)
        # "Add block"
        self.save_customizer_grid(lambda s: s["blocks"].append({"type": "cart_icon", "hidden": False, "settings": {}}))
        shown = icons(self.store_html())
        self.assertGreater(shown, 0)
        self.assertEqual(icons(self.preview_html()), shown)
        # the eye
        self.save_customizer_grid(lambda s: s["blocks"][0].update(hidden=True))
        self.assertEqual(icons(self.store_html()), 0)
        self.save_customizer_grid(lambda s: s["blocks"][0].update(hidden=False))
        self.assertEqual(icons(self.store_html()), shown)
        # delete
        self.save_customizer_grid(lambda s: s["blocks"].clear())
        self.assertEqual([b["type"] for b in self.customizer_grid()["blocks"]], [])
        self.assertEqual(icons(self.store_html()), 0)


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


class PageTemplatesTest(StoreClientMixin, APITestCase):
    """Alternate page templates (page.contact, ...): picker, code editor, and the pages that use them."""

    def setUp(self):
        self.token, self.store = self.sign_up("templates@example.com", "Templates Store")

    def files(self):
        return {f["path"]: f["content"] for f in self.ok("get", "/api/v1/themes/files/")["files"]}

    def keys(self):
        return [t["key"] for t in self.ok("get", "/api/v1/themes/page-templates/")["templates"]]

    def page(self, path, expect=200):
        r = self.client.get(f"/s/{self.store['storeSlug']}/{path}")
        self.assertEqual(r.status_code, expect)
        return r.content.decode()

    def test_starter_templates_are_in_the_picker_and_the_code_editor(self):
        self.assertEqual(self.keys(), ["page", "page.about", "page.contact", "page.faq", "page.policy"])
        files = self.files()
        for key in ("page.about", "page.contact", "page.faq", "page.policy"):
            self.assertIn(f"templates/{key}.liquid", files)
            self.assertIn(f"templates/{key}.json", files)
        settings = self.ok("get", "/api/v1/themes/settings/")
        self.assertIn("page.faq", settings["templates"])

    def test_page_uses_the_template_it_picked(self):
        page = self.ok("post", "/api/v1/themes/pages/", {"title": "Contact", "body": "<p>Hi</p>", "template": "page.contact"}, expect=201)
        self.assertEqual(page["template"], "page.contact")
        self.assertIn("contact-details", self.page("pages/contact"))
        other = self.ok("post", "/api/v1/themes/pages/", {"title": "Plain", "body": "<p>x</p>"}, expect=201)
        self.assertEqual(other["template"], "page.plain")
        self.assertNotIn("contact-details", self.page("pages/plain"))
        self.ok("post", "/api/v1/themes/pages/", {"title": "Bad", "template": "page.nope"}, expect=400)

    def test_new_page_gets_its_own_template_in_the_code_editor(self):
        page = self.ok("post", "/api/v1/themes/pages/", {"title": "Size Guide", "body": "<p>S M L</p>"}, expect=201)
        self.assertEqual(page["template"], "page.size-guide")
        files = self.files()
        self.assertIn("Size Guide", files["templates/page.size-guide.liquid"])
        self.assertIn("{{ sections }}", files["templates/page.size-guide.liquid"])
        self.assertIn("templates/page.size-guide.json", files)
        self.assertIn("S M L", self.page("pages/size-guide"))
        again = self.ok("post", "/api/v1/themes/pages/", {"title": "Size Guide", "body": "<p>2</p>"}, expect=201)
        self.assertEqual(again["template"], "page.size-guide-2")
        self.assertEqual(self.ok("post", "/api/v1/themes/pages/", {"title": "T", "template": "page.faq"}, expect=201)["template"], "page.faq")

    def test_liquid_edited_in_the_code_editor_shows_on_the_store(self):
        self.ok("post", "/api/v1/themes/pages/", {"title": "FAQ", "body": "<p>x</p>", "template": "page.faq"}, expect=201)
        liquid = self.files()["templates/page.faq.liquid"]
        self.ok("post", "/api/v1/themes/files/", {"path": "templates/page.faq.liquid", "content": liquid + "<p id=\"added\">Added</p>"})
        self.assertIn('id="added"', self.page("pages/faq"))
        self.ok("post", "/api/v1/themes/files/", {"path": "templates/page.faq.liquid", "content": "{% if %}"}, expect=400)

    def test_new_template_from_the_editor_appears_in_the_code_editor(self):
        made = self.ok("post", "/api/v1/themes/page-templates/", {"name": "Size Guide"}, expect=201)
        self.assertEqual(made["key"], "page.size-guide")
        files = self.files()
        self.assertIn("templates/page.size-guide.liquid", files)
        self.assertIn("templates/page.size-guide.json", files)
        self.ok("post", "/api/v1/themes/page-templates/", {"name": "Size Guide"}, expect=400)

    def test_customizer_changes_reach_the_json_file_and_back(self):
        self.ok("get", "/api/v1/themes/page-templates/")
        self.ok("patch", "/api/v1/themes/settings/", {"templates": {"page.contact": {"sections": {
            "extra": {"type": "rich-text", "settings": {"heading": "Find us"}}}, "order": ["main", "extra"]}}})
        data = json.loads(self.files()["templates/page.contact.json"])
        self.assertEqual(data["order"], ["main", "extra"])
        data["sections"]["extra"]["settings"]["heading"] = "Visit us"
        self.ok("post", "/api/v1/themes/files/", {"path": "templates/page.contact.json", "content": json.dumps(data)})
        settings = self.ok("get", "/api/v1/themes/settings/")
        self.assertEqual(settings["templates"]["page.contact"]["sections"]["extra"]["settings"]["heading"], "Visit us")

    def test_new_liquid_or_json_file_creates_a_template(self):
        self.ok("post", "/api/v1/themes/files/", {"path": "templates/page.team.liquid", "content": "{{ sections }}"})
        self.assertIn("page.team", self.keys())
        self.ok("post", "/api/v1/themes/files/", {"path": "templates/page.jobs.json",
                "content": json.dumps({"order": ["main"], "sections": {"main": {"type": "main-page", "settings": {}}}})})
        self.assertIn("page.jobs", self.keys())
        self.assertIn("templates/page.jobs.liquid", self.files())

    def test_deleting_a_template_sends_its_pages_back_to_page(self):
        self.ok("post", "/api/v1/themes/pages/", {"title": "FAQ", "body": "<p>x</p>", "template": "page.faq"}, expect=201)
        self.ok("delete", "/api/v1/themes/files/?path=templates/page.faq.liquid")
        self.assertNotIn("page.faq", self.keys())
        pages = self.ok("get", "/api/v1/themes/pages/")
        self.assertEqual(pages[0]["template"], "")
        self.assertIn('class="page__title"', self.page("pages/faq"))
        self.ok("delete", "/api/v1/themes/files/?path=templates/page.liquid", expect=400)
        self.ok("post", "/api/v1/themes/files/rename/", {"source": "templates/page.about.liquid", "target": "templates/page.us.liquid"}, expect=400)

    def test_customizer_previews_a_template(self):
        data = self.ok("get", "/api/v1/themes/settings/")
        html = self.ok("post", "/api/v1/themes/preview/?page=page.contact", data)["html"]
        self.assertIn("contact-details", html)
        self.ok("post", "/api/v1/themes/preview/?page=page.bad_name", data, expect=400)


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
