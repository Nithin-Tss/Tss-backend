"""
Two-way sync of the synced sections: sections/<name>.liquid <-> the customizer's panel.
The header and the footer run the same checks through SyncedSectionMixin.

    python manage.py test apps.themes --settings=config.test_settings
"""
import hashlib
import copy
import json
import re
import time
import uuid
from urllib.parse import unquote

from rest_framework.test import APITestCase

from apps.core.tests.test_flow import StoreClientMixin

from . import section_sync as sync
from .services import get_store_theme


# The starter header's loop over the wishlist and cart icons, in block order
HEADER_ICONS_LOOP = """    {%- assign has_actions = false -%}
    {%- if s.show_search != false or s.show_account != false or wishlist_blocks.size > 0 or cart_blocks.size > 0 -%}{%- assign has_actions = true -%}{%- endif -%}
    {%- if has_actions -%}
      <div class="site-header__actions">
        {%- if s.show_search != false -%}
          <a class="site-header__search" href="{{ shop.url }}/collections/all" aria-label="Search" title="Search">
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
            </svg>
          </a>
        {%- endif -%}
        {%- if s.show_account != false -%}
          <style>.site-header__account:hover { background: transparent; opacity: 1; }</style>
          {%- if customer_logged_in -%}
            <a class="site-header__account" href="{{ shop.url }}/account/" aria-label="Account" title="Account">
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <circle cx="12" cy="12" r="10" /><circle cx="12" cy="10" r="3" /><path d="M7 20.662V19a2 2 0 0 1 2-2h6a2 2 0 0 1 2 2v1.662" />
              </svg>
            </a>
          {%- else -%}
            <a class="site-header__account" href="{{ shop.url }}/account/login" aria-label="Sign in or create account" title="Sign in or create account">
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <circle cx="12" cy="12" r="10" /><circle cx="12" cy="10" r="3" /><path d="M7 20.662V19a2 2 0 0 1 2-2h6a2 2 0 0 1 2 2v1.662" />
              </svg>
            </a>
          {%- endif -%}
        {%- endif -%}
        {%- for block in section.blocks -%}
          {%- if block.type == 'wishlist' -%}
            {% render 'wishlist', mode: 'icon', count: wishlist_count, label: block.settings.label, show_count: block.settings.show_count, attrs: block.attrs %}
          {%- elsif block.type == 'cart' -%}
            {% render 'cart', mode: 'icon', count: cart_count, session: cart_session, settings: block.settings, attrs: block.attrs %}
          {%- elsif block.type == 'profile' -%}
            <a class="site-header__profile" href="{{ block.settings.url | default: '/account' }}"{{ block.attrs }}><img src="{{ block.settings.image }}" alt="" width="{{ block.settings.size }}"></a>
          {%- endif -%}
        {%- endfor -%}
      </div>
    {%- endif -%}
"""


PRE_MEGA_LOOK_CSS = """    /* Mega menu container */
    body .site-header__nav .site-header__item--mega {
      position: static;
    }
    body .site-header__nav .mega-menu {
      position: absolute;
      top: 100%;
      left: 0;
      right: 0;
      width: 100%;
      max-width: 100%;
      background: var(--bg);
      color: var(--text);
      border-top: 1px solid var(--line);
      border-bottom: 1px solid var(--line);
      box-shadow: 0 16px 36px rgba(0, 0, 0, 0.12);
      z-index: 1000;
      opacity: 0;
      visibility: hidden;
      pointer-events: none;
      transition: opacity 0.2s ease, transform 0.2s ease;
      transform: translateY(8px);
      text-align: left;
      box-sizing: border-box;
      overflow-x: clip;
    }
    body .site-header__nav .site-header__item--mega:hover .mega-menu,
    body .site-header__nav .site-header__item--mega.is-open .mega-menu,
    body .site-header__nav .site-header__item--mega:focus-within .mega-menu {
      opacity: 1;
      visibility: visible;
      pointer-events: auto;
      transform: translateY(0);
    }
    body .site-header__nav .mega-menu__inner {
      display: flex;
      gap: 32px;
      padding-top: 24px;
      padding-bottom: 28px;
      align-items: flex-start;
      justify-content: space-between;
      box-sizing: border-box;
      max-width: var(--page);
      width: 100%;
    }
    body .site-header__nav .mega-menu__columns {
      flex: 1 1 auto;
      min-width: 0;
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
      gap: 24px 32px;
      align-content: start;
      max-width: 100%;
    }
    body .site-header__nav .mega-menu__column {
      min-width: 130px;
    }
    body .site-header__nav .mega-menu__column-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 14px;
    }
    body .site-header__nav .mega-menu__column-title,
    body .site-header__nav a.mega-menu__column-title {
      display: flex;
      align-items: center;
      font-size: 13px;
      font-weight: 700;
      color: var(--text);
      text-transform: uppercase;
      letter-spacing: 0.06em;
      opacity: 1;
      border-radius: 0;
      padding: 0;
      margin: 0;
      text-decoration: none;
    }
    body .site-header__nav a.mega-menu__column-title:hover {
      color: var(--accent);
    }
    body .site-header__nav .mega-menu__sublinks {
      list-style: none;
      margin: 0;
      padding: 0;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    body .site-header__nav .mega-menu__sublink-item {
      margin: 0;
      padding: 0;
    }
    body .site-header__nav a.mega-menu__sublink {
      display: inline-flex;
      align-items: center;
      font-size: 13px;
      font-weight: 400;
      color: var(--muted);
      text-decoration: none;
      text-transform: none;
      letter-spacing: normal;
      opacity: 0.85;
      padding: 3px 0;
      border-radius: 0;
      background: transparent;
      transition: color 0.15s ease, opacity 0.15s ease, transform 0.15s ease;
    }
    body .site-header__nav a.mega-menu__sublink:hover {
      color: var(--text);
      opacity: 1;
      transform: translateX(2px);
      background: transparent;
    }
"""

PRE_MEGA_LOOK_MOBILE_CSS = (
    (
        '''      body .site-header__nav .mega-menu__columns {
        display: flex;
        flex-direction: column;
        flex-wrap: nowrap;
        gap: 10px;
        width: 100%;
        padding-left: 0;
      }''',
        '''      body .site-header__nav .mega-menu__columns {
        display: flex;
        flex-direction: column;
        gap: 10px;
        width: 100%;
      }''',
    ),
    (
        '''        padding-bottom: 8px;
        gap: 0;
      }''',
        '''        padding-bottom: 8px;
        gap: 6px;
      }''',
    ),
    (
        '''      body .site-header__nav a.mega-menu__sublink {
        min-height: var(--mega-row, 38px);
        display: flex;
        align-items: center;
        padding: 0;
      }
      /* Shop All sits inside the opened column */
      body .site-header__nav .mega-menu__column--has-sublinks:not(.is-col-open) a.mega-menu__shop-all {
        display: none;
      }
      body .site-header__nav a.mega-menu__shop-all {
        display: flex;
        padding: 0 0 0 14px;
      }''',
        '''      body .site-header__nav a.mega-menu__sublink {
        min-height: 38px;
        display: flex;
        align-items: center;
        padding: 6px 0;
        font-size: 14px;
      }''',
    ),
)


def rev50_header():
    """The starter header of revision 50 (before the ShopNow-style navigation), saved as a fixture."""
    from pathlib import Path

    return (Path(__file__).parent / "test_fixtures" / "header.rev50.liquid").read_text(encoding="utf-8")


def pre_mega_look_header():
    """
    The starter header of revision 48, before the mega menu look settings and the column "Shop All" link: the revision 50
    starter (a saved fixture, so it does not move with the current starter) with those changes taken out. Its hash is
    registered in STARTER_UPGRADES.
    """
    import re

    header = rev50_header()
    # the --mega-* assigns at the top and the variables on the <header>
    start = header.index("{%- comment -%}\n  Mega menu look:")
    end = header.index("{%- assign mega_shop_all_label")
    header = header[:start] + header[header.index("\n", end) + 1:]
    start = header.index("  {% if mega_bg != blank %}--mega-bg")
    header = header[:start] + header[header.index("{%- endcapture -%}", start):]
    # "Shop All" in every column, and the heading that links only when Shop All is off
    header, shop_all = re.subn(
        r"[ \t]*\{%- if mega_shop_all and col\.url != blank and col\.url != '#' -%\}\n.*?\{%- endif -%\}\n", "", header, flags=re.S)
    assert shop_all == 2
    assert header.count(" and mega_shop_all == false") == 2
    header = header.replace(" and mega_shop_all == false", "")
    # the CSS of the mega menu panel and its mobile accordion
    start, end = header.index("    /* Mega menu container"), header.index("    /* Promo banner */")
    header = header[:start] + PRE_MEGA_LOOK_CSS + header[end:]
    for new, old in PRE_MEGA_LOOK_MOBILE_CSS:
        assert header.count(new) == 1, new[:60]
        header = header.replace(new, old)
    # the settings
    start = header.index(',\n    {"type": "color", "id": "mega_bg"')
    return header[:start] + header[header.index("\n  ],", start):]


def pre_rotation_header():
    """The starter header from before the rotating announcement bar (revision 47): one message and four options."""
    header = pre_mega_look_header()
    bar_start = header.index("{%- comment -%}\n  Announcement bar:")
    bar_end = header.index('<header class="site-header"')
    settings_start = header.index('    {"type": "text", "id": "announcement_text"')
    settings_end = header.index('    {"type": "text", "id": "logo_text"')
    block_start = header.index('    {\n      "type": "announcement",')
    block_end = header.index('    {\n      "type": "link",')
    return (
        header[:bar_start]
        + """{%- if s.announcement_text != blank -%}
  {%- capture announcement_style -%}
    {% if s.announcement_bg != blank %}background:{{ s.announcement_bg }};{% endif %}
    {% if s.announcement_color != blank %}color:{{ s.announcement_color }};{% endif %}
  {%- endcapture -%}
  {%- if s.announcement_link != blank -%}
    <a class="announcement announcement--link" href="{{ s.announcement_link | link }}" style="{{ announcement_style | strip }}">{{ s.announcement_text }}</a>
  {%- else -%}
    <div class="announcement" style="{{ announcement_style | strip }}">{{ s.announcement_text }}</div>
  {%- endif -%}
{%- endif -%}
"""
        + header[bar_end:settings_start]
        + """    {"type": "text", "id": "announcement_text", "label": "Announcement text", "default": "", "info": "A banner above the header. Empty hides it."},
    {"type": "url", "id": "announcement_link", "label": "Announcement link", "default": "", "info": "Optional. Makes the whole banner a link."},
    {"type": "color", "id": "announcement_bg", "label": "Announcement colour", "default": "", "info": "None uses the accent colour."},
    {"type": "color", "id": "announcement_color", "label": "Announcement text colour", "default": ""},
"""
        + header[settings_end:block_start]
        + header[block_end:]
    )


def wishlist_era_header():
    """The starter header from before the cart icon: the heart as a block, and no cart (what revision 20 stores have)."""
    from . import section_sync as sync
    header = pre_rotation_header()
    loop_start = header.index("    {%- assign has_actions = false -%}")
    loop_end = header.index("    {%- endif -%}\n  </div>\n</header>", loop_start) + len("    {%- endif -%}\n")
    header_icons_loop = header[loop_start:loop_end]
    for new, old in (
        ("""  The "Cart icon" block shows the cart with the number of items. It opens the side drawer or the cart
  page: everything the cart does is in snippets/cart.liquid.
""", ""),
        ("{%- assign cart_blocks = section.blocks | where: 'type', 'cart' -%}\n", ""),
        (header_icons_loop, sync.WISHLIST_BLOCK_MARKUP.split("\n", 1)[1]),
        ("    },\n    " + sync.CART_BLOCK_SCHEMA + "\n  ],", "    }\n  ],"),
        ("    " + sync.SEARCH_BLOCK_SCHEMA + ",\n", ""),
        (', {"type": "cart", "settings": {}}]}', "]}"),
        ('"blocks": [{"type": "search", "settings": {"label": "Search", "placeholder": "Search products"}}, ', '"blocks": ['),
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

        broken = good.replace('"max_blocks": 30,', '"max_blocks": 30')
        self.assertNotEqual(broken, good)
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
        values["blocks"] = [{"type": "social", "settings": {"icons": "instagram | javascript:alert(1)"}}]
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


class FooterBlocksTest(SyncedSectionMixin, APITestCase):
    """The owner-controlled footer: blocks (logo, menu columns, social icons, ...), the bottom strip, the mobile accordion."""

    NAME = "footer"
    EMAIL = "footer-blocks@example.com"
    timings = {"sidebar -> code + preview": [], "code save -> blocks + preview": []}

    def values(self, blocks=None, **settings):
        values = self.section()["values"]
        values["settings"].update(settings)
        if blocks is not None:
            values["blocks"] = blocks
        return values

    def column(self, heading, menu, **extra):
        return {"type": "links", "settings": {"heading": heading, "menu": menu, **extra}}

    def base(self):
        return f"/s/{self.store['storeSlug']}"

    def footer_columns(self, html):
        """How many menu columns are accordions (the attribute on the element, not the CSS and script that name it)."""
        return len(re.findall(r"<nav[^>]* data-footer-col", html))

    def home_after(self, blocks, **settings):
        self.save_values(self.values(blocks, **settings))
        return self.home()

    def test_a_new_store_starts_with_the_default_footer(self):
        home = self.home()
        for text in ("Explore", "Offer services", "Other Services", "Shop all", 'aria-label="Social links"',
                     'aria-label="Instagram"', "Privacy Policy", "Tell shoppers about your store."):
            self.assertIn(text, home)
        self.assertIn(f"© {time.strftime('%Y')} Footer Store", home.replace("&copy;", "©"))
        self.assertEqual([b["type"] for b in self.section()["values"]["blocks"]], ["logo", "links", "links", "links", "social"])

    def test_the_page_has_one_footer_landmark(self):
        home = self.home()
        self.assertEqual(home.count("<footer"), 1)
        self.assertEqual(home.count("</footer>"), 1)
        blocks = [{"type": "logo", "settings": {}}, self.column("A", "x | /a"),
                  {"type": "social", "settings": {"icons": "facebook | https://facebook.com/x"}},
                  {"type": "newsletter", "settings": {}}, {"type": "payment", "settings": {}}]
        self.assertEqual(self.home_after(blocks).count("<footer"), 1)

    def test_blocks_can_be_added_in_any_number_duplicated_reordered_and_removed(self):
        column = self.column("Column", "One | /one")
        blocks = [{"type": "logo", "settings": {}}] + [
            self.column(f"Col {i}", f"Link {i} | /pages/p{i}") for i in range(8)] + [column, dict(column)]
        saved, preview = self.save_values(self.values(blocks))
        self.assertEqual(len(saved["values"]["blocks"]), 11)
        headings = re.findall(r'class="site-footer__heading"[^>]*>([^<]+)<', preview)
        self.assertEqual(headings, [f"Col {i}" for i in range(8)] + ["Column", "Column"])

        reordered = self.section()["values"]["blocks"]
        reordered.reverse()
        reordered.pop()  # removes the logo, now last
        _, preview = self.save_values(self.values(reordered))
        self.assertLess(preview.index("Column"), preview.index("Col 7"))
        self.assertLess(preview.index("Col 7"), preview.index("Col 0"))
        defaults = self.schema_of(self.code())["default"]["blocks"]
        self.assertEqual([b["type"] for b in defaults], ["links"] * 10)  # written into the code, in this order
        self.assertEqual(defaults[0]["settings"]["heading"], "Column")

    def test_menu_column_with_typed_links(self):
        home = self.home_after([self.column(
            "Shop", "Everything | /collections/all\nA product | /products/abc\nSite | https://example.com/x")])
        self.assertIn(f'<a href="{self.base()}/collections/all">Everything</a>', home)
        self.assertIn(f'href="{self.base()}/products/abc"', home)
        self.assertIn('href="https://example.com/x"', home)

    def test_menu_column_follows_a_menu_from_admin_menus(self):
        menu = self.ok("post", "/api/v1/themes/menus/", {"name": "Footer links", "items": [
            {"title": "Our story", "url": "/pages/about"}, {"title": "Journal", "url": "/blogs/news"}]}, expect=201)
        column = {"type": "links", "settings": {"heading": "Company", "source": "menu", "menu_ref": menu["handle"],
                                                "menu": "Typed | /typed"}}
        home = self.home_after([column])
        self.assertIn(f'href="{self.base()}/pages/about">Our story</a>', home)
        self.assertLess(home.index("Our story"), home.index("Journal"))
        self.assertNotIn("Typed", home)  # the menu is used instead of the typed links

        # The menu changes: the footer follows it, with no change to the footer
        self.ok("put", f"/api/v1/themes/menus/{menu['id']}/", {"name": "Footer links", "items": [
            {"title": "Careers", "url": "/pages/careers"}, {"title": "Our story", "url": "/pages/about"}]})
        home = self.home()
        self.assertIn("Careers", home)
        self.assertNotIn("Journal", home)
        self.assertLess(home.index("Careers"), home.index("Our story"))

        # Found by id as well, and the customizer's preview shows it too
        column["settings"]["menu_ref"] = menu["id"]
        _, preview = self.save_values(self.values([column]))
        self.assertIn("Careers", preview)

    def test_menu_column_lists_sublinks_of_a_heading_without_a_link(self):
        menu = self.ok("post", "/api/v1/themes/menus/", {"name": "Grouped", "items": [
            {"title": "Help", "url": "", "children": [{"title": "Delivery", "url": "/pages/delivery"}]},
            {"title": "Home", "url": "/"}]}, expect=201)
        column = {"type": "links", "settings": {"heading": "Info", "source": "menu", "menu_ref": menu["id"]}}
        home = self.home_after([column])
        self.assertIn(">Delivery</a>", home)
        self.assertIn(">Home</a>", home)
        self.assertNotIn(">Help</a>", home)

    def test_menu_column_with_a_missing_menu_or_another_stores_menu_shows_nothing(self):
        mine = (self.token, self.store["storeId"])
        _, other = self.sign_up("other-menu@example.com", "Other Menu Store")
        private = self.ok("post", "/api/v1/themes/menus/", {"name": "Private", "items": [
            {"title": "Secret link", "url": "/secret"}]}, expect=201)
        self.use(*mine)
        for ref in ("no-such-menu", private["handle"], private["id"]):
            column = {"type": "links", "settings": {"heading": "Menu", "source": "menu", "menu_ref": ref}}
            self.assertNotIn("Secret link", self.home_after([column]), ref)

    def test_social_links_are_icons_side_by_side_in_new_tabs(self):
        icons = "\n".join([
            "facebook | https://facebook.com/shop", "instagram | https://instagram.com/shop", "youtube | https://youtube.com/@shop",
            "tiktok | https://tiktok.com/@shop", "x | https://x.com/shop", "threads | https://threads.net/@shop",
            "linkedin | https://linkedin.com/company/shop", "pinterest | https://pinterest.com/shop",
            "snapchat | https://snapchat.com/add/shop", "whatsapp | https://wa.me/919999900000",
            "telegram | https://t.me/shop", "bluesky | https://bsky.app/profile/shop",
            "custom | https://example.com/me | My site | https://example.com/icon.png"])
        block = {"type": "social", "settings": {"icons": icons, "size": 28, "shape": "square", "border": False,
                                                 "color": "#112233", "background": "#EEEEEE", "border_color": "#FF0000"}}
        home = self.home_after([block])
        self.assertEqual(home.count('target="_blank" rel="noopener noreferrer"'), 13)
        for name in ("Facebook", "Instagram", "YouTube", "TikTok", "X", "Threads", "LinkedIn", "Pinterest", "Snapchat",
                     "WhatsApp", "Telegram", "Bluesky", "My site"):
            self.assertIn(f'aria-label="{name}"', home)
        self.assertIn('<img src="https://example.com/icon.png"', home)
        self.assertEqual(home.count('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"'), 12)
        self.assertIn("--sf-icon:28px", home)
        self.assertIn("--sf-icon-color:#112233", home)
        self.assertIn("--sf-icon-bg:#EEEEEE", home)
        self.assertIn("--sf-icon-border:#FF0000", home)
        self.assertIn('data-shape="square"', home)
        self.assertIn('data-border="false"', home)
        self.assertEqual(home.count('class="site-footer__social"'), 1)  # one list, its items in a row
        self.assertEqual(home.count("<li><a href"), 13)
        self.assertIn("display: flex; flex-direction: row; flex-wrap: wrap", home)

    def test_social_links_with_unsafe_addresses_are_refused(self):
        for bad in ("javascript:alert(1)", "data:text/html,x", "//evil.example"):
            block = {"type": "social", "settings": {"icons": f"custom | {bad}"}}
            self.ok("patch", "/api/v1/themes/settings/", {"sections": {"footer": self.values([block])}}, expect=400)
        block = {"type": "social", "settings": {"icons": "custom | https://ok.example | x | javascript:alert(1)"}}
        self.ok("patch", "/api/v1/themes/settings/", {"sections": {"footer": self.values([block])}}, expect=400)

    def test_social_text_is_escaped_unknown_networks_are_custom_and_the_list_is_capped(self):
        icons = ("custom | https://a.example | <script>x()</script>\nmyspace | https://b.example\n"
                 + "\n".join(f"custom | https://e.example/{i}" for i in range(40)))
        home = self.home_after([{"type": "social", "settings": {"icons": icons}}])
        self.assertNotIn("<script>x()</script>", home)
        self.assertEqual(home.count('target="_blank"'), sync.MAX_SOCIAL)
        self.assertIn('aria-label="b.example"', home)

    def test_bottom_strip_copyright_tokens_and_chosen_policies(self):
        home = self.home_after([], copyright="© {year} {store} - made in India", policy_privacy=False, policy_refund=True,
                               policy_shipping=True, policy_terms=False, divider_color="#AABBCC")
        self.assertIn(f"© {time.strftime('%Y')} Footer Store - made in India", home.replace("&copy;", "©"))
        self.assertIn("/pages/refund-policy", home)
        self.assertIn("/pages/shipping-policy", home)
        self.assertNotIn("/pages/privacy-policy", home)
        self.assertNotIn("/pages/terms-of-service", home)
        self.assertIn("--sf-divider:#AABBCC", home)
        home = self.home_after([], policy_privacy=True, policy_terms=True)
        self.assertIn("/pages/privacy-policy", home)
        self.assertIn("/pages/terms-of-service", home)
        home = self.home_after([], show_copyright=False, policy_privacy=False, policy_terms=False, policy_refund=False,
                               policy_shipping=False)
        self.assertNotIn('<div class="page-width site-footer__legal">', home)

    def test_section_style_settings_reach_the_page(self):
        settings = dict(background="#101010", text_color="#DDDDDD", heading_color="#FFFFFF", link_color="#BBBBBB",
                        link_hover_color="#FFCC00", heading_size=18, heading_case="uppercase", heading_spacing=1.5,
                        link_size=15, column_gap=40, padding_top=60, padding_bottom=20, max_width=1200, text_align="center")
        home = self.home_after([self.column("Shop", "All | /collections/all")], **settings)
        footer = home[home.index("<footer"):home.index("</footer>")]
        for fragment in ("background:#101010", "color:#DDDDDD", "--sf-hover:#FFCC00", "padding-top:60px", "padding-bottom:20px",
                         "text-align:center", "max-width:1200px", "gap:40px"):
            self.assertIn(fragment, footer)
        self.assertIn('data-hover="true"', footer)
        heading = re.search(r'<h2 class="site-footer__heading" style="([^"]*)"', footer).group(1)
        for fragment in ("color:#FFFFFF", "font-size:18px", "text-transform:uppercase", "letter-spacing:1.5px"):
            self.assertIn(fragment, heading)
        link = re.search(r'<a href="[^"]*/collections/all" style="([^"]*)"', footer).group(1)
        self.assertIn("color:#BBBBBB", link)
        self.assertIn("font-size:15px", link)

    def test_the_logo_column_is_wider_in_the_wide_layout_only(self):
        blocks = [{"type": "logo", "settings": {}}, self.column("A", "a | /a"), self.column("B", "b | /b"), self.column("C", "c | /c")]
        home = self.home_after(blocks)
        self.assertIn('data-wide-first="true"', home)
        self.assertIn("--sf-cols:3", home)
        self.assertIn("grid-template-columns: 1.45fr repeat(var(--sf-cols), minmax(0, 1fr))", home)
        self.assertIn('data-wide-first="false"', self.home_after(blocks, layout="equal"))
        self.assertIn('data-wide-first="false"', self.home_after(blocks[1:]))  # no brand column in front

    def test_mobile_accordion_markup_and_setting(self):
        home = self.home()
        self.assertIn('data-accordion="true"', home)
        self.assertEqual(self.footer_columns(home), 3)  # the three menu columns, not the logo column
        self.assertIn('id="footer-panel-2"', home)
        for fragment in ("aria-expanded", "aria-controls", "site-footer__toggle", "createElement('button')", "max-width: 640px"):
            self.assertIn(fragment, home)
        brand = home[home.index('class="site-footer__brand'):]
        self.assertNotIn("data-footer-col", brand[:brand.index("</div>")])
        off = self.home_after(self.section()["values"]["blocks"], accordion_mobile=False)
        self.assertIn('data-accordion="false"', off)
        self.assertNotIn("createElement('button')", off)

    def test_a_column_without_a_heading_is_not_an_accordion(self):
        self.assertEqual(self.footer_columns(self.home_after([self.column("", "a | /a")])), 0)

    def test_text_newsletter_and_payment_blocks(self):
        blocks = [{"type": "text", "settings": {"heading": "About", "text": "Hand made"}},
                  {"type": "newsletter", "settings": {"heading": "Join", "text": "Offers", "button_label": "Sign up"}},
                  {"type": "payment", "settings": {"heading": "We accept", "methods": "Visa, UPI ,, RuPay"}}]
        home = self.home_after(blocks)
        self.assertIn("Hand made", home)
        self.assertIn(">Sign up</button>", home)
        payments = home[home.index('aria-label="Payment methods"'):]
        self.assertEqual(re.findall(r"<li>([^<]*)</li>", payments[:payments.index("</ul>")]), ["Visa", "UPI", "RuPay"])

    def test_the_new_settings_go_to_the_code_and_back(self):
        block = {"type": "social", "settings": {"icons": "facebook | https://facebook.com/a\ninstagram | https://instagram.com/a"}}
        column = {"type": "links", "settings": {"heading": "Co", "source": "menu", "menu_ref": "main-menu"}}
        self.save_values(self.values([column, block], link_hover_color="#FF00AA"))
        schema = self.schema_of(self.code())
        defaults = schema["default"]["blocks"]
        self.assertEqual(defaults[0]["settings"]["menu_ref"], "main-menu")
        self.assertIn("instagram | https://instagram.com/a", defaults[1]["settings"]["icons"])
        self.assertEqual(next(s for s in schema["settings"] if s["id"] == "link_hover_color")["default"], "#FF00AA")
        # Edit code: one more social link in the schema's defaults shows in the customizer and on the page
        edited = self.code().replace("instagram | https://instagram.com/a", "instagram | https://instagram.com/a\\ntelegram | https://t.me/a")
        self.assertNotEqual(edited, self.code())
        self.save_code(edited)
        self.assertIn("telegram | https://t.me/a", self.section()["values"]["blocks"][1]["settings"]["icons"])
        self.assertIn('aria-label="Telegram"', self.home())

    def test_the_customizer_offers_every_block_and_setting(self):
        spec = self.section()["spec"]
        self.assertEqual(set(spec["blocks"]), {"logo", "links", "social", "text", "newsletter", "payment"})
        self.assertEqual(spec["blocks"]["logo"]["name"], "Logo and text")
        self.assertEqual(spec["blocks"]["links"]["name"], "Menu column")
        menu_ref = spec["blocks"]["links"]["settings"]["menu_ref"]
        self.assertEqual((menu_ref["type"], menu_ref["show_if"]), ("menu_ref", {"source": "menu"}))
        self.assertEqual(spec["blocks"]["links"]["settings"]["menu"]["type"], "menu")
        self.assertEqual(spec["blocks"]["social"]["settings"]["icons"]["type"], "social")
        self.assertGreaterEqual(spec["max_blocks"], 30)
        for key in ("accordion_mobile", "link_hover_color", "heading_size", "heading_case", "heading_spacing", "link_size",
                    "column_gap", "padding_top", "padding_bottom", "max_width", "layout", "text_align", "background",
                    "heading_color", "link_color", "text_color", "divider_color", "copyright", "policy_privacy"):
            self.assertIn(key, spec["settings"])
        self.assertTrue(spec["settings"]["accordion_mobile"]["default"])


class FooterUpgradeTest(StoreClientMixin, APITestCase):
    """Starter footers move to the block footer; edited footers and their saved content stay."""

    def setUp(self):
        from apps.tenancy.models import Store
        from .services import get_store_theme

        _, store = self.sign_up("footer-upgrade@example.com", "Chegondis Store")
        self.store = Store.objects.get(store_id=store["storeId"])
        self.slug = store["storeSlug"]
        self.theme = get_store_theme(self.store).theme

    def footer_file(self):
        from .models import ThemeFile

        return ThemeFile.objects.get(theme=self.theme, file_path="sections/footer.liquid")

    def theme_data(self, **changes):
        data = dict(self.theme.__class__.objects.get(pk=self.theme.pk).theme_data)
        for key, value in changes.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        self.theme.theme_data = data
        self.theme.save()
        return data

    def set_footer(self, content, **theme_data):
        file = self.footer_file()
        file.content = content
        file.save()
        self.theme_data(**theme_data)

    def test_the_previous_starter_footers_are_known_as_untouched(self):
        from .services import STARTER_UPGRADES

        for digest in ("aed60ddb600d8c22f5c29b97a8898a9abcaa3dccc1114878e288acaa9d11a0da",
                       "fe2823e8cfb36ada85b3091d01914fe232bab461e6d7918df2bc78deb9c57802"):
            self.assertIn(digest, STARTER_UPGRADES["sections/footer.liquid"])
            self.assertIn(digest, sync.SYNCED_SECTIONS["footer"]["originals"])
        self.assertGreaterEqual(sync.ORIGINALS_REVISION, 16)

    def test_an_untouched_old_starter_footer_is_replaced_and_an_edited_one_is_not(self):
        from unittest import mock

        from . import services
        from .services import starter_files

        old = '{% schema %}{"name": "Footer", "settings": []}{% endschema %}<footer>old</footer>'
        upgrades = {**services.STARTER_UPGRADES, "sections/footer.liquid": {hashlib.sha256(old.encode()).hexdigest()}}
        with mock.patch.object(services, "STARTER_UPGRADES", upgrades):
            self.set_footer(old, starter_revision=1)
            get_store_theme(self.store)
            self.assertEqual(self.footer_file().content, starter_files()["sections/footer.liquid"])

            edited = old.replace("old", "mine")
            self.set_footer(edited, starter_revision=1)
            get_store_theme(self.store)
            self.assertEqual(self.footer_file().content, edited)

    def legacy_values(self):
        return {
            "settings": {"copyright": "© Chegondis", "background": "#0F2E24", "text_color": "#EAEAEA"},
            "blocks": [
                {"type": "text", "settings": {"heading": "Chegondis", "text": "Fresh from the farm"}},
                {"type": "links", "settings": {"heading": "Explore", "menu": "Home | /\nShop all | /collections/all"}},
                {"type": "links", "settings": {"heading": "Offer services", "menu": "Offers | /collections/promotions"}},
                {"type": "links", "settings": {"heading": "Other Services", "menu": "FAQ | /pages/faq"}},
                {"type": "social", "settings": {"instagram": "https://instagram.com/chegondis",
                                                 "facebook": "https://facebook.com/chegondis",
                                                 "youtube": "https://youtube.com/@chegondis"}},
            ],
        }

    def save_legacy_footer(self):
        store_theme = get_store_theme(self.store)
        store_theme.custom_data = {**store_theme.custom_data, "sections": {"footer": self.legacy_values()}}
        store_theme.save()
        self.theme_data(footer_social_icons=None)

    def test_a_stores_saved_footer_keeps_its_content_and_social_links_after_the_upgrade(self):
        self.save_legacy_footer()
        saved = get_store_theme(self.store).custom_data["sections"]["footer"]
        social = saved["blocks"][4]["settings"]
        self.assertEqual(social["icons"], "instagram | https://instagram.com/chegondis\n"
                                          "facebook | https://facebook.com/chegondis\nyoutube | https://youtube.com/@chegondis")
        self.assertNotIn("instagram", social)
        self.assertEqual(saved["blocks"][:4], self.legacy_values()["blocks"][:4])

        home = self.client.get(f"/s/{self.slug}/").content.decode()
        footer = home[home.index("<footer"):]
        order = [footer.index(text) for text in ("Fresh from the farm", ">Explore<", ">Offer services<", ">Other Services<",
                                                'aria-label="Instagram"', 'aria-label="Facebook"', 'aria-label="YouTube"',
                                                "© Chegondis")]
        self.assertEqual(order, sorted(order))
        self.assertIn("background:#0F2E24", home)
        self.assertIn(f'href="/s/{self.slug}/collections/all">Shop all</a>', home)
        self.assertEqual(home.count("<footer"), 1)

    def test_the_conversion_runs_once(self):
        self.save_legacy_footer()
        get_store_theme(self.store)
        self.assertEqual(self.theme_data()["footer_social_icons"], 1)
        store_theme = get_store_theme(self.store)
        store_theme.custom_data = {**store_theme.custom_data, "sections": {"footer": self.legacy_values()}}
        store_theme.save()
        get_store_theme(self.store)
        self.assertIn("instagram", get_store_theme(self.store).custom_data["sections"]["footer"]["blocks"][4]["settings"])

    def test_an_edited_old_footer_keeps_its_own_settings(self):
        self.save_legacy_footer()
        edited = self.footer_file().content.replace('"id": "icons"', '"id": "icon_list"')
        self.assertNotEqual(edited, self.footer_file().content)
        self.set_footer(edited, starter_revision=10 ** 6)
        get_store_theme(self.store)
        saved = get_store_theme(self.store).custom_data["sections"]["footer"]
        self.assertEqual(saved["blocks"][4]["settings"]["instagram"], "https://instagram.com/chegondis")
        self.assertNotIn("footer_social_icons", self.theme_data())


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
        self.assertEqual(sync.split(sync.remove_search_render_branch(before) or before)[0].rstrip("\n"),
                         sync.split(after)[0].rstrip("\n"))  # only the Search branch was removed
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
        self.assertNotIn('data-tss-section="', self.home())  # (the stock script only names it in a selector)

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
        self.assertEqual([b["type"] for b in header["values"]["blocks"]], ["wishlist", "cart", "search"])
        self.assertEqual(header["spec"]["blocks"]["search"]["name"], "Search")
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
        self.assertEqual(set(spec), {"link", "search", "wishlist", "cart", "announcement"})  # what "Add block" offers

    def test_search_block_add_remove_reorder_and_two_way_code_sync(self):
        header = self.section()
        self.assertEqual(header["spec"]["blocks"]["search"]["name"], "Search")
        values = header["values"]
        search = next(block for block in values["blocks"] if block["type"] == "search")
        search_index = values["blocks"].index(search)
        search["settings"].update(label="Find products", placeholder="Type a product name")
        _, preview = self.save_values(values, header["version"])
        self.assertIn(f'data-tss-block="{search_index}" data-tss-label="Search"', preview)
        self.assertIn('aria-label="Find products"', preview)
        self.assertIn('placeholder="Type a product name"', preview)

        values["blocks"] = [
            next(block for block in values["blocks"] if block["type"] == "wishlist"),
            search,
            next(block for block in values["blocks"] if block["type"] == "cart"),
        ]
        _, preview = self.save_values(values)
        self.assertEqual(self.default_block_types(), ["wishlist", "search", "cart"])
        self.assertLess(preview.index('data-tss-label="Wishlist icon"'), preview.index('data-tss-label="Search"'))

        # Edit code adds a schema field: the existing Search block remains in the sidebar with its saved values.
        code = self.with_schema(self.code(), lambda schema: (
            next(block for block in schema["blocks"] if block["type"] == "search").update(name="Header search"),
            next(block for block in schema["blocks"] if block["type"] == "search")["settings"].append(
                {"type": "text", "id": "hint", "label": "Input hint", "default": "Type to search"}),
        ))
        saved = self.save_code(code)
        self.assertEqual(saved["sync"]["spec"]["blocks"]["search"]["name"], "Header search")
        current = self.section()
        block = next(item for item in current["values"]["blocks"] if item["type"] == "search")
        self.assertEqual(block["settings"]["placeholder"], "Type a product name")
        self.assertEqual(block["settings"]["hint"], "Type to search")

        current["values"]["blocks"] = [item for item in current["values"]["blocks"] if item["type"] != "search"]
        _, preview = self.save_values(current["values"], current["version"])
        self.assertNotIn("data-header-search", preview)
        self.assertNotIn('class="site-header__search"', preview)
        self.assertNotIn('class="site-header__search"', self.home())
        self.assertNotIn("search", self.default_block_types())
        self.assertIn("search", self.section()["spec"]["blocks"])
        self.assertNotIn("data-header-search", self.code())

    def test_search_block_schema_field_saved_in_customizer_updates_code(self):
        header = self.section()
        block = next(item for item in header["values"]["blocks"] if item["type"] == "search")
        block["settings"]["placeholder"] = "Find something"
        self.save_values(header["values"], header["version"])
        default_search = next(item for item in self.schema_of(self.code())["default"]["blocks"] if item["type"] == "search")
        self.assertEqual(default_search["settings"]["placeholder"], "Find something")

    def test_search_render_branch_removed_in_code_keeps_add_block_but_hides_icon(self):
        header = self.section()
        self.assertIn("search", header["spec"]["blocks"])
        branchless = sync.remove_search_render_branch(self.code())
        self.assertIsNotNone(branchless)
        self.save_code(branchless)
        current = self.section()
        self.assertIn("search", current["spec"]["blocks"])
        self.assertTrue(any(block["type"] == "search" for block in current["values"]["blocks"]))
        self.assertNotIn("data-header-search", self.home())
        self.assertNotIn('class="site-header__search"', self.home())

    def test_pasted_branchless_header_keeps_search_schema_and_saved_block(self):
        original = self.section()
        original_blocks = original["values"]["blocks"]
        branchless = sync.remove_search_render_branch(self.code())
        pasted = self.with_schema(branchless, lambda schema: (
            schema.update(blocks=[b for b in schema["blocks"] if b["type"] != "search"]),
            schema["default"].update(
                blocks=[b for b in schema["default"]["blocks"] if b["type"] != "search"]),
        ))

        saved = self.save_code(pasted)
        self.assertIn("search", saved["sync"]["spec"]["blocks"])
        self.assertTrue(any(b["type"] == "search" for b in self.schema_of(self.code())["blocks"]))
        self.assertEqual(self.section()["values"]["blocks"], original_blocks)
        self.assertNotIn("data-header-search", self.code())
        self.assertNotIn('class="site-header__search"', self.header_html(self.home()))

        values = self.section()["values"]
        values["blocks"] = [b for b in values["blocks"] if b["type"] != "search"]
        _, _ = self.save_values(values)
        self.assertFalse(any(b["type"] == "search" for b in self.section()["values"]["blocks"]))
        values = self.section()["values"]
        values["blocks"].append({"type": "search", "settings": {"label": "Search", "placeholder": "Search products"}})
        _, preview = self.save_values(values)
        self.assertTrue(any(b["type"] == "search" for b in self.section()["values"]["blocks"]))
        self.assertIn("data-header-search", self.code())
        self.assertEqual(preview.count('class="site-header__search"'), 1)

    def test_customizer_add_restores_missing_search_render_branch_once(self):
        values = self.section()["values"]
        values["blocks"] = [block for block in values["blocks"] if block["type"] != "search"]
        self.save_values(values)
        self.assertNotIn("data-header-search", self.code())

        values = self.section()["values"]
        values["blocks"].append({"type": "search", "settings": {"label": "Find", "placeholder": "Find items"}})
        _, preview = self.save_values(values)
        self.assertIn("data-header-search", self.code())
        self.assertEqual(preview.count('class="site-header__search"'), 1)
        self.assertEqual(self.header_html(self.home()).count('class="site-header__search"'), 1)

    def test_search_block_added_to_schema_in_code_appears_in_add_block(self):
        content = self.code()
        original = next(block for block in self.schema_of(content)["blocks"] if block["type"] == "search")
        without_search = self.with_schema(content, lambda schema: schema.update(
            blocks=[block for block in schema["blocks"] if block["type"] != "search"]))
        self.save_code(without_search)
        self.assertNotIn("search", self.section()["spec"]["blocks"])
        with_search = self.with_schema(self.code(), lambda schema: schema["blocks"].append(original))
        saved = self.save_code(with_search)
        self.assertEqual(saved["sync"]["spec"]["blocks"]["search"]["name"], "Search")
        self.assertIn("search", self.section()["spec"]["blocks"])

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
        bad = re.sub(r"({%-?\s*(?:if|elsif)\s+block\.type == 'wishlist'\s*-?%})",
                     r"\1{%- if -%}", good, count=1)
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
        self.assertEqual(self.default_block_types(), ["wishlist", "search", "cart"])
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
        for name in ("Autumn", "Basics", "Coats"):  # each has a product to buy: the automatic menu skips empty ones
            product = self.ok("post", "/api/v1/catalog/products/", {
                "title": f"{name} thing", "status": "active", "price": "10", "sku": f"AUTO-{name.upper()}",
            }, expect=201)
            self.ok("post", "/api/v1/catalog/collections/",
                    {"name": name, "status": "active", "products": [product["id"]]}, expect=201)
        self.ok("post", "/api/v1/catalog/collections/", {"name": "Barely there", "status": "active"}, expect=201)
        values = self.section()["values"]
        self.assertEqual([b["type"] for b in values["blocks"]], ["wishlist", "cart", "search"])  # only the icons: no menu links
        values["settings"]["collections"] = 2
        _, preview = self.save_values(values)
        nav = self.header_html(self.home())
        self.assertIn(">Home</a>", nav)
        self.assertIn(">Shop all</a>", nav)
        shown = [name for name in ("Autumn", "Basics", "Coats") if f">{name}</a>" in nav]
        self.assertEqual(shown, ["Autumn", "Basics"])
        self.assertNotIn(">Barely there</a>", nav)  # no products in it
        # The fast preview path reads the collections the same way
        self.assertEqual(shown, [name for name in ("Autumn", "Basics", "Coats") if f">{name}</a>" in preview])

    def test_menu_from_admin_menus_shows_on_the_navbar(self):
        self.ok("post", "/api/v1/themes/menus/", {"name": "Main menu", "items": [
            {"title": "Sale", "url": "/collections/all"}, {"title": "Journal", "url": "/blog"}]}, expect=201)
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
        self.assertEqual([b["settings"]["label"] for b in values["blocks"]], ["About", "Sale", "Wishlist", "Cart", "Search"])
        self.assertEqual([b["type"] for b in values["blocks"]], ["link", "link", "wishlist", "cart", "search"])
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
        self.a = self.product("Product A", "500", images=["/media/a.jpg"])
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
            self.assertIn('src="/media/a.jpg"', html)
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

    def test_removing_a_line_that_is_already_gone_refreshes_instead_of_failing(self):
        self.add(self.mug, 1)
        html = self.call("get", "fragment").json()["html"]
        line = __import__("re").search(r'data-line="([0-9a-f-]{36})"', html).group(1)
        self.call("post", "change", {"line": line, "quantity": 0})  # removed in another tab
        again = self.call("post", "change", {"line": line, "quantity": 0})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["count"], 0)
        self.assertEqual(self.call("post", "change", {"line": line, "quantity": 2}).status_code, 400)

    def line_id(self):
        return __import__("re").search(r'data-line="([0-9a-f-]{36})"', self.call("get", "fragment").json()["html"]).group(1)

    def set_stock(self, product, available):
        from django.db.models import F

        from apps.inventory.models import InventoryItem

        InventoryItem.objects.filter(variant_id=product["variants"][0]["id"]).update(
            available=available, on_hand=available + F("committed") + F("unavailable"))

    def test_change_refuses_bad_quantities_and_lines(self):
        self.add(self.mug, 2)
        line = self.line_id()
        for bad in (-1, 1.5, "2", None, True, 100):
            r = self.call("post", "change", {"line": line, "quantity": bad})
            self.assertEqual(r.status_code, 400, bad)
        self.assertEqual(self.call("post", "change", {"line": line}).status_code, 400)  # no quantity
        self.assertEqual(self.call("post", "change", {"line": "nope", "quantity": 1}).status_code, 400)
        self.assertEqual(self.call("post", "change", {"line": str(uuid.uuid4()), "quantity": 1}).status_code, 400)
        self.assertEqual(self.call("post", "change", {"line": line, "quantity": 1}, session="abc").status_code, 400)  # no cart
        self.assertEqual(self.call("get", "change").status_code, 400)
        self.assertEqual(self.call("get", "fragment").json()["count"], 2)  # none of it changed the cart

    def test_change_cannot_go_over_the_stock_but_can_always_go_down_or_remove(self):
        self.add(self.mug, 4)  # 5 in stock
        line = self.line_id()
        over = self.call("post", "change", {"line": line, "quantity": 6})
        self.assertEqual(over.status_code, 400)
        self.assertIn("Only 5", over.json()["error"])
        self.assertEqual(self.call("post", "change", {"line": line, "quantity": 5}).json()["count"], 5)
        # stock falls below what is in the cart (sold elsewhere): the shopper must still be able to reduce it
        self.set_stock(self.mug, 2)
        down = self.call("post", "change", {"line": line, "quantity": 3})
        self.assertEqual((down.status_code, down.json()["count"]), (200, 3))
        self.assertIn("Only 2 left", down.json()["html"])  # still more than is in stock: the line says so
        self.assertEqual(self.call("post", "change", {"line": line, "quantity": 4}).status_code, 400)  # not up
        self.assertEqual(self.call("post", "change", {"line": line, "quantity": 2}).json()["count"], 2)
        # sold out altogether: it can still be removed
        self.set_stock(self.mug, 0)
        self.assertEqual(self.call("post", "change", {"line": line, "quantity": 1}).json()["count"], 1)
        self.assertEqual(self.call("post", "change", {"line": line, "quantity": 0}).json()["count"], 0)

    def test_change_to_the_same_quantity_and_other_lines_stay_put(self):
        other = self.product("Clay bowl", "BOWL-1", 5)
        self.add(self.mug, 2)
        self.add(other, 1)
        html = self.call("get", "fragment").json()["html"]
        lines = __import__("re").findall(r'data-line="([0-9a-f-]{36})"', html)
        self.assertEqual(len(lines), 2)
        same = self.call("post", "change", {"line": lines[0], "quantity": 2}).json()
        self.assertEqual((same["count"], same["subtotal"]), (3, "36.00"))
        gone = self.call("post", "change", {"line": lines[0], "quantity": 0}).json()
        self.assertEqual((gone["count"], gone["subtotal"]), (1, "12.00"))
        self.assertIn("Clay bowl", gone["html"])
        self.assertNotIn("Clay mug", gone["html"])

    def test_clear_empties_the_cart(self):
        other = self.product("Clay bowl", "BOWL-2", 5)
        self.add(self.mug, 2)
        self.add(other, 1)
        self.assertIn("data-cart-clear", self.call("get", "fragment").json()["html"])  # the page offers it
        cleared = self.call("post", "clear").json()
        self.assertEqual((cleared["count"], cleared["subtotal"]), (0, "0.00"))
        self.assertIn("Your cart is empty", cleared["html"])
        self.assertNotIn("data-cart-clear", cleared["html"])
        self.assertEqual(self.count_in(self.header()), 0)
        again = self.call("post", "clear")  # nothing to clear, or no cart at all: fine
        self.assertEqual((again.status_code, again.json()["count"]), (200, 0))
        self.assertEqual(self.call("post", "clear", session="abc").status_code, 200)
        self.assertEqual(self.call("get", "clear").status_code, 400)
        self.assertEqual(self.add(self.mug, 1).json()["count"], 1)  # the cart works again afterwards

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
        # another shopper's line is not theirs to change: removing it is a harmless no-op (their cart has no such
        # line), and anything else is refused
        self.assertEqual(taken.status_code, 200)
        self.assertEqual(taken.json()["count"], 0)
        self.assertEqual(self.call("post", "change", {"line": line, "quantity": 5}, session=other, client=stranger).status_code, 400)
        self.assertEqual(self.call("post", "clear", session=other, client=stranger).json()["count"], 0)
        self.assertEqual(self.call("get", "fragment").json()["count"], 2)  # the owner's cart is untouched
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
        header = self.ok("get", "/api/v1/themes/sections/")["header"]["values"]
        values = header
        cart = next(b for b in values["blocks"] if b["type"] == "cart")
        cart["settings"]["checkout_url"] = "javascript:alert(1)"
        r = self.client.patch("/api/v1/themes/settings/", {"sections": {"header": values}}, format="json")
        self.assertEqual(r.status_code, 400, r.content[:300])

    def test_checkout_button_follows_the_owner_setting(self):
        self.add(self.mug, 1)
        html = self.call("get", "fragment").json()["html"]
        self.assertIn("data-cart-checkout-soon", html)  # no address set: "Checkout is coming soon"
        header = self.ok("get", "/api/v1/themes/sections/")["header"]["values"]
        values = header
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
        header = self.ok("get", "/api/v1/themes/sections/")["header"]["values"]
        values = header
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
        from .services import STARTER_REVISION

        self.revert_to_revision_20()
        self.assertFalse(self.theme().theme.files.filter(file_path="snippets/other").exists())
        theme = self.theme().theme
        self.assertTrue(theme.files.filter(file_path=self.SNIPPET).exists())
        self.assertEqual(theme.theme_data["starter_revision"], 56)
        self.assertIn("{% render 'cart', mode: 'icon'", self.stored("sections/header.liquid"))
        self.assertIn("{% render 'cart', mode: 'script' %}", self.stored("sections/main-product.liquid"))
        self.assertIsNotNone(hashlib)

    def old_cart_snippet(self):
        """The cart snippet as the first revision shipped it (before the script kept a header's own badge up to date)."""
        current = self.snippet_before_link_ids()
        start = current.index("    // A header that draws its own cart icon")
        end = current.index("    });\n", start) + len("    });\n")
        return current[:start] + current[end:]

    def snippet_before_clear_cart(self):
        """The snippet as revision 37 shipped it: no "Clear cart" button and no handler for it."""
        from .services import starter_files

        current = starter_files()[self.SNIPPET]
        button = '\n      <button class="cart-clear" type="button" data-cart-clear>Clear cart</button>'
        self.assertIn(button, current)
        older = current.replace(button, "", 1)
        start = older.index('    var clear = target.closest("[data-cart-clear]");')
        end = older.index('    if (target.closest("[data-cart-checkout-soon]")) {')
        return older[:start] + older[end:]

    def register_as_older(self, *texts):
        """The older copies are rebuilt from the current snippet, so their hashes move whenever the snippet changes.
        Register them for this test only; the shipped hashes in STARTER_UPGRADES are covered by the constants test."""
        import hashlib
        from unittest import mock

        from .services import STARTER_UPGRADES

        hashes = {hashlib.sha256(t.encode("utf-8")).hexdigest() for t in texts}
        patcher = mock.patch.dict(STARTER_UPGRADES, {self.SNIPPET: STARTER_UPGRADES[self.SNIPPET] | hashes})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_snippet_from_before_clear_cart_is_registered_for_the_upgrade(self):
        import hashlib

        from .services import STARTER_UPGRADES, starter_files

        text = self.snippet_before_clear_cart()
        self.assertNotIn("data-cart-clear", text)
        self.register_as_older(text, text.replace("\n", "\r\n"))  # an LF copy and a CRLF copy
        theme = self.theme().theme
        theme.files.filter(file_path=self.SNIPPET).update(content=text, file_size=len(text.encode("utf-8")))
        theme.theme_data = {**theme.theme_data, "starter_revision": 37}
        theme.save()
        self.theme()
        self.assertEqual(self.stored(self.SNIPPET), starter_files()[self.SNIPPET])
        self.assertIn("data-cart-clear", self.stored(self.SNIPPET))

    def test_the_snippet_from_before_the_checkout_redirect_is_registered_for_the_upgrade(self):
        """A store holding it showed "Checkout is coming soon" on the real store instead of opening checkout."""
        import hashlib

        from .services import STARTER_UPGRADES, starter_files

        hashes = STARTER_UPGRADES[self.SNIPPET]
        self.assertIn("bf6180d6c1db029759afe489ea7f753f99897e16d0394f296736abe101704a31", hashes)
        self.assertIn("3bd8efbcc5e448be5369a998b85d83c64c02916442ada043303971153dda4e7c", hashes)
        # the current snippet must never be one of the "older" copies, or it would be replaced by itself
        current = starter_files()[self.SNIPPET]
        self.assertNotIn(hashlib.sha256(current.encode("utf-8")).hexdigest(), hashes)
        self.assertIn('window.location.href = href;', current)

    def snippet_before_link_ids(self):
        """The snippet as revision 22 shipped it: the script kept a header's own badge, but links had no cart id."""
        from .services import starter_files

        current = self.snippet_before_clear_cart()
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
        self.register_as_older(text)
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
        self.register_as_older(old)
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


class HeaderSearchBlockPatchTest(StoreClientMixin, APITestCase):
    def setUp(self):
        self.token, self.store = self.sign_up("headersearchpatch@example.com", "Search Patch Store")

    def theme(self):
        from apps.tenancy.models import Store
        from .services import get_store_theme

        return get_store_theme(Store.objects.get(store_id=self.store["storeId"]))

    def stored(self):
        from .models import Theme

        return Theme.objects.get(slug=f"store-{self.store['storeId']}").files.get(file_path="sections/header.liquid").content

    def install(self, content, values=None):
        store_theme = self.theme()
        theme = store_theme.theme
        theme.files.filter(file_path="sections/header.liquid").update(content=content, file_size=len(content.encode("utf-8")))
        theme.theme_data = {k: v for k, v in theme.theme_data.items() if k != "search_block_offered"}
        theme.save()
        if values is not None:
            store_theme.custom_data = {**store_theme.custom_data, "sections": {"header": values}}
            store_theme.save(update_fields=["custom_data", "updated_at"])

    def test_untouched_starter_hash_is_registered_and_search_is_default(self):
        import hashlib

        from .services import STARTER_REVISION, STARTER_UPGRADES, starter_files

        self.assertEqual(STARTER_REVISION, 56)
        self.assertIn(
            "a4042599d876c80166e60601ed1a2763f264e79678ad99552f46c233402b1e49",
            STARTER_UPGRADES["sections/header.liquid"],
        )
        schema = sync.parse(starter_files()["sections/header.liquid"])
        self.assertIn("search", [block["type"] for block in schema["default"]["blocks"]])

    def test_untouched_header_upgrade_replaces_code_and_appends_search_to_saved_values(self):
        import hashlib
        from unittest import mock

        from . import services

        store_theme = self.theme()
        theme = store_theme.theme
        old_header = edited_header()
        saved_values = {
            "settings": {"logo_text": "Keep this", "layout": "center"},
            "blocks": [{"type": "link", "settings": {"label": "Old link", "url": "/pages/old"}}],
        }
        store_theme.custom_data = {**store_theme.custom_data, "sections": {"header": saved_values}}
        store_theme.save(update_fields=["custom_data", "updated_at"])
        theme.files.filter(file_path="sections/header.liquid").update(
            content=old_header, file_size=len(old_header.encode("utf-8")),
        )
        theme.theme_data = {**theme.theme_data, "starter_revision": 40, "search_block_offered": 0}
        theme.save()

        current_header = services.starter_files()["sections/header.liquid"]
        old_digest = hashlib.sha256(old_header.encode("utf-8")).hexdigest()
        with mock.patch.object(services, "STARTER_UPGRADES", {"sections/header.liquid": {old_digest}}), \
             mock.patch.object(services, "starter_files", return_value={"sections/header.liquid": current_header}):
            services._upgrade_untouched_starter_files(theme)
        self.assertEqual(self.stored(), current_header)
        services._offer_search_block(store_theme)
        self.assertEqual(store_theme.custom_data["sections"]["header"]["settings"], saved_values["settings"])
        blocks = store_theme.custom_data["sections"]["header"]["blocks"]
        self.assertEqual(blocks[0], saved_values["blocks"][0])
        self.assertEqual([block["type"] for block in blocks], ["link", "search"])
        self.assertEqual(sync.history_of(theme, "header")[0]["preview"], old_header[:160])

    def test_edited_header_gets_search_by_insertion_and_keeps_saved_values_and_history(self):
        header = edited_header()
        values = {
            "settings": {"logo_text": "My shop", "layout": "center"},
            "blocks": [
                {"type": "link", "settings": {"label": "About", "url": "/pages/about"}},
                {"type": "profile", "settings": {"image": "", "size": 42, "url": "/account"}},
            ],
        }
        self.install(header, values)
        store_theme = self.theme()
        patched = self.stored()
        self.assertIn("owner note: keep this line", patched)
        self.assertIn("site-header__account", patched)
        self.assertIn("data-header-search", patched)
        self.assertIn("{{ block.attrs }}", patched)
        self.assertEqual(sync.history_of(store_theme.theme, "header")[0]["preview"], header[:160])
        saved = store_theme.custom_data["sections"]["header"]
        self.assertEqual(saved["settings"], values["settings"])
        self.assertEqual(saved["blocks"][:2], values["blocks"])
        self.assertEqual(saved["blocks"][2]["type"], "search")

    def test_upgrade_removes_the_old_show_search_link_when_search_block_already_exists(self):
        from . import services
        from .services import starter_files

        store_theme = self.theme()
        theme = store_theme.theme
        legacy_control = '''        {%- if s.show_search != false -%}
          <a class="site-header__search" href="{{ shop.url }}/collections/all" aria-label="Search" title="Search">
            <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="8" /></svg>
          </a>
        {%- endif -%}
'''
        before = starter_files()["sections/header.liquid"].replace(
            '        {%- if s.show_account != false -%}', legacy_control + '        {%- if s.show_account != false -%}', 1
        )
        self.install(before, {"settings": {"show_search": True}, "blocks": [
            {"type": "search", "settings": {"label": "Search", "placeholder": "Search products"}},
            {"type": "wishlist", "settings": {"label": "Wishlist", "show_count": True}},
            {"type": "cart", "settings": {}},
        ]})
        theme.refresh_from_db()
        services._offer_search_block(store_theme)
        store_theme.theme.refresh_from_db()
        services._offer_search_block(store_theme)  # a repeated upgrade is a no-op

        upgraded = self.stored()
        self.assertNotIn('href="{{ shop.url }}/collections/all" aria-label="Search"', upgraded)
        self.assertEqual(upgraded.count('class="site-header__search"'), 1)
        history = sync.history_of(theme, "header")
        self.assertTrue(history)
        self.assertIn("s.show_search", sync.history_content(theme, "header", 0))
        response = self.client.get(f"/s/{self.store['storeSlug']}/")
        self.assertEqual(response.status_code, 200)
        storefront_header = response.content.decode().split("</header>", 1)[0]
        self.assertEqual(storefront_header.count('class="site-header__search"'), 1)

    def test_duplicate_search_blocks_in_defaults_and_saved_values_are_deduped_once(self):
        from . import services
        from .services import starter_files

        store_theme = self.theme()
        theme = store_theme.theme
        header = starter_files()["sections/header.liquid"]
        schema = sync.parse(header)
        schema["default"]["blocks"].append(copy.deepcopy(schema["default"]["blocks"][0]))
        header = sync.write_values(header, sync.defaults_of(sync.spec_of("header", schema), schema))
        search = {"type": "search", "settings": {"label": "Find", "placeholder": "Find products"}}
        saved = {"settings": {"logo_text": "Keep me"}, "blocks": [copy.deepcopy(search), copy.deepcopy(search)]}
        self.install(header, saved)
        theme.refresh_from_db()
        store_theme.refresh_from_db()

        services._offer_search_block(store_theme)
        store_theme.theme.refresh_from_db()
        services._offer_search_block(store_theme)
        store_theme.refresh_from_db()

        upgraded_schema = sync.parse(self.stored())
        self.assertEqual([b["type"] for b in upgraded_schema["default"]["blocks"]].count("search"), 1)
        self.assertEqual([b["type"] for b in store_theme.custom_data["sections"]["header"]["blocks"]].count("search"), 1)
        response = self.client.get(f"/s/{self.store['storeSlug']}/")
        self.assertEqual(response.status_code, 200)
        storefront_header = response.content.decode().split("</header>", 1)[0]
        self.assertEqual(storefront_header.count('class="site-header__search"'), 1)

    def test_search_panel_positioning_is_anchored_and_viewport_clamped(self):
        from . import rendering

        self.assertIn("getBoundingClientRect()", sync.SEARCH_BLOCK_BRANCH)
        self.assertIn("desiredLeft = iconRect.right - width", sync.SEARCH_BLOCK_BRANCH)
        self.assertIn("left - rootRect.left", sync.SEARCH_BLOCK_BRANCH)
        self.assertIn("panel.style.right = '0'", sync.SEARCH_BLOCK_BRANCH)
        self.assertIn("window.innerWidth - width - 12", sync.SEARCH_BLOCK_BRANCH)
        self.assertIn("input.focus()", sync.SEARCH_BLOCK_BRANCH)
        self.assertIn(".site-header__search-block{position:relative", rendering.SECTION_CSS)
        self.assertIn(".site-header__search-panel{position:absolute;top:100%;right:0", rendering.SECTION_CSS)
        self.assertIn("width:min(92vw,520px)", rendering.SECTION_CSS)
        self.assertIn(".tss-wrap:has(.site-header__search-block){overflow:visible;transform:none!important}", rendering.SECTION_CSS)

    def test_custom_header_without_safe_insertion_points_is_left_untouched(self):
        from .services import get_store_theme
        from apps.tenancy.models import Store

        custom = '''<header class="my-custom-header"><a class="site-header__search" href="/collections/all">Search</a><p>Keep my layout</p></header>
{% schema %}
{"name":"Header","settings":[],"blocks":[{"type":"link","name":"Menu link","settings":[]}],"default":{"blocks":[]}}
{% endschema %}
'''
        self.install(custom, {"settings": {}, "blocks": [{"type": "link", "settings": {}}]})
        store_theme = self.theme()
        history_before = sync.history_of(store_theme.theme, "header")
        self.assertEqual(self.stored(), custom)
        self.assertEqual(store_theme.custom_data["sections"]["header"]["blocks"], [{"type": "link", "settings": {}}])
        self.assertEqual(sync.history_of(store_theme.theme, "header"), history_before)


class StorefrontWishlistTest(StoreClientMixin, APITestCase):
    def setUp(self):
        self.token, self.store = self.sign_up("wishlist@example.com", "Wishlist Store")
        location = self.ok("post", "/api/v1/inventory/locations/", {"name": "Main"}, expect=201)["id"]
        self.product = self.ok("post", "/api/v1/catalog/products/", {  # in stock: the card shows Add to cart, not Sold out
            "title": "Blue mug", "sku": "SKU-MUG", "status": "active", "price": "12.50",
            "location": location, "quantity": 5,
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
        self.assertIn(".product-list__grid, .wishlist-page__grid { display: grid;", html)

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
        # Wishlist cards use the shared square image box, not the old portrait ratio
        wishlist_grid = html[html.index('class="wishlist-page__grid"'):]
        self.assertNotIn("product-card__media--portrait", wishlist_grid)
        self.assertIn(".product-list__grid, .wishlist-page__grid { display: grid;", html)
        self.assertIn(".product-card__wishlist-add.is-wishlisted svg", html)
        self.assertIn('.product-card__wishlist-add[aria-pressed="true"] svg', html)
        self.assertIn('.product__wishlist-add[aria-pressed="true"] svg', html)
        self.assertIn('button.closest("form[data-wishlist-form]")', html)  # the script now lives in snippets/wishlist.liquid
        self.assertNotIn('product-card__wishlist-remove', html)  # the filled heart removes, no Remove button
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
        self.assertNotIn('data-wishlist-remove', page)
        self.assertIn('class="product-card__wishlist-add is-wishlisted"', page)

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
        # every store link (not only the wishlist one) and the page's own address carry the id, and Back re-fetches
        script = starter_files()["snippets/wishlist.liquid"]
        self.assertIn('document.querySelectorAll("a[href]").forEach(tag)', script)
        self.assertIn("history.replaceState", script)
        self.assertIn("event.persisted", script)
        # and a click can remove it again
        gone = APIClient().post(f"/s/{slug}/wishlist/items/", {
            "action": "remove", "product_id": self.product["id"], "cart": cart}, HTTP_ACCEPT="application/json")
        self.assertEqual(gone.json()["count"], 0)
        self.assertNotIn("Blue mug", APIClient().get(f"/s/{slug}/wishlist/?cart={cart}").content.decode())

    def test_a_signed_in_customer_wishlist_is_saved_in_the_wishlist_tables(self):
        """A heart clicked by a signed-in customer (from the sandbox: cart id only) becomes a wishlist_items row."""
        import re

        from django.contrib.auth import get_user_model
        from django.http import HttpResponse
        from rest_framework.test import APIClient

        from apps.tenancy.models import Store
        from apps.wishlist.models import Wishlist, WishlistItem

        from .views import set_storefront_customer_cookie

        slug = self.store["storeSlug"]
        user, _ = get_user_model().objects.get_or_create(
            email="wish-cust@example.com", defaults={"first_name": "Ada", "last_name": "Lovelace"})
        dummy = HttpResponse()
        set_storefront_customer_cookie(dummy, Store.objects.get(pk=self.store["storeId"]), user)
        cookie = dummy.cookies[f"tss_cust_{slug}"].value

        # the page load carries the customer's cookie and gives the page its cart id
        shopper = APIClient()
        shopper.cookies[f"tss_cust_{slug}"] = cookie
        home = shopper.get(f"/s/{slug}/").content.decode()
        cart = re.search(r'data-cart-session="([0-9a-f]{32})"', home).group(1)

        # the click: no cookie, only the cart id
        saved = APIClient().post(f"/s/{slug}/wishlist/items/", {
            "action": "add", "product_id": self.product["id"], "cart": cart}, HTTP_ACCEPT="application/json")
        self.assertEqual(saved.json()["count"], 1)
        item = WishlistItem.objects.get(wishlist__store_id=self.store["storeId"])
        self.assertEqual(str(item.product_id), self.product["id"])
        self.assertEqual(item.wishlist.customer.email, "wish-cust@example.com")

        # adding it twice keeps one row; the page shows it
        APIClient().post(f"/s/{slug}/wishlist/items/", {
            "action": "add", "product_id": self.product["id"], "cart": cart}, HTTP_ACCEPT="application/json")
        self.assertEqual(WishlistItem.objects.count(), 1)
        self.assertIn("Blue mug", shopper.get(f"/s/{slug}/wishlist/?cart={cart}").content.decode())

        # removing deletes the row (the customer keeps their wishlist)
        gone = APIClient().post(f"/s/{slug}/wishlist/items/", {
            "action": "remove", "product_id": self.product["id"], "cart": cart}, HTTP_ACCEPT="application/json")
        self.assertEqual(gone.json()["count"], 0)
        self.assertEqual(WishlistItem.objects.count(), 0)
        self.assertEqual(Wishlist.objects.count(), 1)

        # signed out (a page load without the cookie): the same cart id is a guest again, nothing reaches the tables
        APIClient().get(f"/s/{slug}/?cart={cart}")
        APIClient().post(f"/s/{slug}/wishlist/items/", {
            "action": "add", "product_id": self.product["id"], "cart": cart}, HTTP_ACCEPT="application/json")
        self.assertEqual(WishlistItem.objects.count(), 0)

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
        self.assertNotIn('class="product-card__wishlist-remove"', page)
        self.assertIn('class="product-card__wishlist-add is-wishlisted"', page)  # the filled heart removes it
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
    from pathlib import Path

    # The starter's product section as of revision 39 (before tier cards, image zoom and badges): the
    # edits below undo what the cart and the heart block added to that version
    product = (Path(__file__).parent / "test_fixtures" / "main-product.rev39.liquid").read_text(encoding="utf-8")
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
        data = {k: v for k, v in theme.theme_data.items() if k not in {"wishlist_blocks", "search_block_offered"}}
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
        from .services import STARTER_REVISION, STARTER_UPGRADES

        self.make_store_from_before_the_wishlist_blocks()
        # the fixtures really are the old starter files (their hashes are registered for the upgrade)
        for path in ("sections/header.liquid", "sections/main-product.liquid"):
            digest = hashlib.sha256(self.stored(path).encode("utf-8")).hexdigest()
            self.assertIn(digest, STARTER_UPGRADES[path])

        store_theme = self.theme()
        self.assertEqual(store_theme.theme.theme_data["starter_revision"], 56)
        self.assertIn("{% render 'wishlist', mode: 'icon'", self.file("sections/header.liquid"))
        self.assertIn("section.wishlist_heart", self.file("sections/main-product.liquid"))
        self.assertIsNotNone(self.file(self.SNIPPET))
        # the replaced header is kept in its history (Restore)
        self.assertTrue(sync.history_of(store_theme.theme, "header"))

        # the header keeps the owner's link and gets the heart as a block; "show_wishlist" is gone
        header = self.ok("get", "/api/v1/themes/sections/")["header"]
        self.assertEqual([b["type"] for b in header["values"]["blocks"]], ["link", "wishlist", "search"])
        self.assertEqual(header["values"]["settings"]["logo_text"], "Old shop")
        self.assertEqual([b["type"] for b in sync.parse(self.file("sections/header.liquid"))["default"]["blocks"]],
                         ["link", "wishlist", "search"])
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
                "</form></div>\n")
        self.theme().theme.files.filter(file_path="snippets/product-card.liquid").update(content=mine, file_size=len(mine))
        self.assertEqual(self.file("snippets/product-card.liquid"), mine)  # the upgrade does not touch it
        self.client.post(f"{self.base}/wishlist/items/", {"action": "add", "product_id": self.product["id"]})
        page = self.client.get(f"{self.base}/wishlist/").content.decode()
        self.assertIn("<!-- my card -->", page)
        self.assertIn("Clay mug", page)
        self.assertNotIn('class="product-card__wishlist-remove"', page)

    def test_a_header_and_product_section_the_owner_edited_are_left_alone(self):
        from . import section_sync as sync

        self.make_store_from_before_the_wishlist_blocks(edited=("sections/header.liquid", "sections/main-product.liquid"))
        header, product = self.stored("sections/header.liquid"), self.stored("sections/main-product.liquid")
        store_theme = self.theme()
        upgraded_header = self.file("sections/header.liquid")
        self.assertNotEqual(upgraded_header, header)
        self.assertIn("<!-- mine -->", upgraded_header)
        self.assertIn("data-header-search", upgraded_header)
        self.assertTrue(sync.history_of(store_theme.theme, "header"))
        self.assertEqual(self.file("sections/main-product.liquid"), product)
        values = self.ok("get", "/api/v1/themes/sections/")["header"]["values"]
        self.assertEqual([b["type"] for b in values["blocks"]], ["link", "search"])
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

    def test_preview_script_hands_the_product_back_link_to_the_viewer(self):
        data = self.ok("get", "/api/v1/themes/settings/")
        html = self.ok("post", "/api/v1/themes/preview/?page=product", data)["html"]
        # "< Back" is not followed as a store link (that always opened /collections/all): the viewer
        # that keeps the visited pages is asked to go back
        self.assertIn("[data-product-back]", html)
        self.assertIn('type: "back"', html)
        self.assertLess(html.index('type: "back"'), html.index("var page = storePage(e);"))

    def test_collection_preview_reads_the_page_number_from_page_number(self):
        data = self.ok("get", "/api/v1/themes/settings/")
        # ?page= names the template in the preview, so a storefront ?page=2 arrives as page_number
        response = self.client.post("/api/v1/themes/preview/?page=collection&page_number=2&sort=newest", data, format="json")
        self.assertEqual(response.status_code, 200)

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
        for section in ("product-grid", "product-carousel", "main-collection", "promotions"):
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
THEME_CSS = "snippets/theme-css.liquid"  # the design tokens live here; styles.liquid renders it
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
        original = self.files()[THEME_CSS]
        old_rule, new_rule = "--card-title-weight: 400;", "--card-title-weight: 700;"  # the editable card variable
        self.assertIn(old_rule, original)
        self.save_file(THEME_CSS, original.replace(old_rule, new_rule))  # the temporary change
        self.assertIn(new_rule, self.files()[THEME_CSS])  # what reopening the code editor shows
        self.assertIn(new_rule, self.preview_html())
        for _ in range(2):  # the storefront, and again after a refresh
            html = self.store_html()
            self.assertIn(new_rule, html)
            self.assertIn('class="product-card__title"', html)
        self.save_file(THEME_CSS, original)  # restored
        self.assertIn(old_rule, self.store_html())
        self.assertNotIn(new_rule, self.store_html())

    def test_theme_colours_fill_the_css_variables(self):
        html = self.store_html()
        for line in ("--accent: #1A1916;", "--bg: #FAF9F6;", "--text: #1A1916;"):  # the defaults
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
        self.assertIn("{{ settings.accent_color }}", self.files()[THEME_CSS])  # the CSS file itself is not rewritten

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
        self.assertIn("--b-r:6px", css)  # the theme's default (slight) button corners

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
            self.assertLess(html.index("--accent: #1A1916;"), html.index("--accent:#FF5500"))
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
        # "contact" is reserved for the contact-information policy, so the page gets another handle
        self.assertIn("contact-details", self.page(f"pages/{page['handle']}"))
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


class ThemeLibraryTest(StoreClientMixin, APITestCase):
    """A store's themes: ready-made designs, one published and the rest drafts."""

    def setUp(self):
        self.token, self.store = self.sign_up("library@example.com", "Library Store")

    # ---------------------------------------------------------------- helpers

    def library(self):
        return self.ok("get", "/api/v1/themes/library/")

    def add(self, source="blank", name=None, expect=201):
        body = {"source": source}
        if name:
            body["name"] = name
        return self.ok("post", "/api/v1/themes/library/", body, expect=expect)

    def editing(self, theme_id, method, url, data=None):
        """A theme API call that edits the given theme (X-Theme-Id), like the customizer and code editor."""
        kwargs = {"HTTP_X_THEME_ID": theme_id}
        r = getattr(self.client, method)(url, data, format="json", **kwargs) if data is not None \
            else getattr(self.client, method)(url, **kwargs)
        self.assertEqual(r.status_code, 200, r.content[:300])
        return r.json()

    def code(self, theme_id, path):
        files = self.editing(theme_id, "get", "/api/v1/themes/files/")["files"]
        return next((f["content"] for f in files if f["path"] == path), None)

    def home(self):
        r = self.client.get(f"/s/{self.store['storeSlug']}/")
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    # ---------------------------------------------------------------- presets

    def test_every_preset_is_valid_and_different(self):
        from .presets import build, check_files, list_presets, preset_files

        presets = list_presets()
        self.assertGreaterEqual(len(presets), 5)
        styles = set()
        for preset in presets:
            with self.subTest(preset["slug"]):
                check_files(preset_files(preset["slug"]))
                files, custom_data, theme_data = build(preset["slug"])
                self.assertEqual(theme_data, {"preset": preset["slug"]})
                self.assertIn("templates/index.liquid", files)
                styles.add(files["layout/theme.liquid"] + files.get("snippets/theme-styles.liquid", ""))
        self.assertEqual(len(styles), len(presets))  # every design has its own Liquid files (Classic is the starter's)

    def test_library_lists_presets_and_no_themes_before_the_first_is_added(self):
        data = self.library()
        self.assertEqual(data["themes"], [])
        self.assertGreaterEqual(len(data["presets"]), 5)
        self.assertEqual(self.library()["themes"], [])  # looking installs nothing

    # ---------------------------------------------------------------- published and drafts

    def test_the_first_theme_is_published_and_later_ones_are_drafts(self):
        first = self.add("bold")
        self.assertTrue(first["published"])
        self.assertEqual(first["name"], "Bold")
        self.assertEqual(first["preset"], "bold")

        second = self.add("boutique", name="Summer look")
        self.assertFalse(second["published"])
        self.assertEqual(second["name"], "Summer look")

        themes = self.library()["themes"]
        self.assertEqual([t["published"] for t in themes], [True, False])
        self.assertEqual(sum(t["published"] for t in themes), 1)

    def test_adding_a_theme_does_not_touch_the_live_one(self):
        live = self.add("bold")
        before_home = self.home()  # the first render fills in the header and footer values
        before_code = self.code(live["id"], "snippets/styles.liquid")
        before_settings = self.editing(live["id"], "get", "/api/v1/themes/settings/")

        draft = self.add("fresh")
        self.add("blank", name="Mine")

        self.assertEqual(self.code(live["id"], "snippets/styles.liquid"), before_code)
        self.assertEqual(self.editing(live["id"], "get", "/api/v1/themes/settings/"), before_settings)
        self.assertEqual(self.home(), before_home)
        self.assertIsNotNone(self.code(draft["id"], "snippets/theme-styles.liquid"))

    def test_each_design_has_its_own_look_and_layout(self):
        bold = self.add("bold")
        boutique = self.add("boutique")
        self.assertIn("Bold design", self.code(bold["id"], "snippets/theme-styles.liquid"))
        self.assertIn("theme-styles", self.code(bold["id"], "layout/theme.liquid"))
        self.assertIn("Boutique design", self.code(boutique["id"], "snippets/theme-styles.liquid"))
        self.assertIn("split-hero", self.code(boutique["id"], "sections/hero.liquid"))
        self.assertNotIn("split-hero", self.code(bold["id"], "sections/hero.liquid"))
        self.assertIn("bold-bar", self.code(bold["id"], "sections/product-grid.liquid"))
        self.assertNotIn("bold-bar", self.code(boutique["id"], "sections/product-grid.liquid"))
        fresh = self.add("fresh")
        self.assertIn("fresh-hero", self.code(fresh["id"], "sections/hero.liquid"))
        orders = {}
        for item in (bold, boutique):
            settings = self.editing(item["id"], "get", "/api/v1/themes/settings/")
            orders[item["preset"]] = settings["templates"]["index"]["order"]
        self.assertNotEqual(orders["bold"], orders["boutique"])

    def test_editing_a_draft_leaves_the_published_theme_alone(self):
        live = self.add("classic")
        draft = self.add("minimal")
        before_home = self.home()
        live_settings = self.editing(live["id"], "get", "/api/v1/themes/settings/")
        live_styles = self.code(live["id"], "snippets/styles.liquid")

        self.editing(draft["id"], "patch", "/api/v1/themes/settings/", {"settings": {"accent_color": "#FF0000"}})
        mine = self.code(draft["id"], "snippets/styles.liquid") + "\n<!-- mine -->"
        self.editing(draft["id"], "post", "/api/v1/themes/files/", {"path": "snippets/styles.liquid", "content": mine})

        self.assertEqual(self.editing(draft["id"], "get", "/api/v1/themes/settings/")["settings"]["accent_color"], "#FF0000")
        self.assertEqual(self.editing(live["id"], "get", "/api/v1/themes/settings/"), live_settings)
        self.assertEqual(self.code(live["id"], "snippets/styles.liquid"), live_styles)
        # Without X-Theme-Id the admin edits the published theme
        self.assertEqual(self.ok("get", "/api/v1/themes/settings/"), live_settings)
        self.assertEqual(self.home(), before_home)

    def test_the_draft_can_be_previewed(self):
        self.add("classic")
        draft = self.add("fresh", name="Draft fresh")
        data = self.editing(draft["id"], "get", "/api/v1/themes/settings/")
        r = self.client.post("/api/v1/themes/preview/", data, format="json", HTTP_X_THEME_ID=draft["id"])
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertIn("theme-fresh", r.json()["html"])
        self.assertNotIn("theme-fresh", self.home())

    def test_publishing_swaps_the_live_theme_and_the_old_one_becomes_a_draft(self):
        live = self.add("classic")
        draft = self.add("fresh")
        self.assertNotIn("theme-fresh", self.home())

        self.ok("post", f"/api/v1/themes/library/{draft['id']}/publish/", {})
        published = {t["id"]: t["published"] for t in self.library()["themes"]}
        self.assertEqual(published, {live["id"]: False, draft["id"]: True})
        self.assertIn("theme-fresh", self.home())
        # The old theme is untouched and still there to edit or publish again
        self.assertIsNotNone(self.code(live["id"], "snippets/styles.liquid"))
        self.ok("post", f"/api/v1/themes/library/{live['id']}/publish/", {})
        self.assertNotIn("theme-fresh", self.home())

    # ---------------------------------------------------------------- manage

    def test_rename_and_delete(self):
        live = self.add("classic")
        draft = self.add("bold")

        self.ok("patch", f"/api/v1/themes/library/{draft['id']}/", {"name": "  Black   Friday "})
        self.assertIn("Black Friday", [t["name"] for t in self.library()["themes"]])
        self.ok("patch", f"/api/v1/themes/library/{draft['id']}/", {"name": " "}, expect=400)

        self.ok("delete", f"/api/v1/themes/library/{live['id']}/", expect=400)  # published
        self.ok("delete", f"/api/v1/themes/library/{draft['id']}/", expect=204)
        self.assertEqual([t["id"] for t in self.library()["themes"]], [live["id"]])

    def test_duplicate_a_theme(self):
        live = self.add("bold")
        self.editing(live["id"], "patch", "/api/v1/themes/settings/", {"settings": {"accent_color": "#123456"}})
        copy = self.add(live["id"], name="Bold copy")
        self.assertFalse(copy["published"])
        self.assertEqual(copy["preset"], "bold")
        self.assertEqual(self.editing(copy["id"], "get", "/api/v1/themes/settings/")["settings"]["accent_color"], "#123456")

    def test_unknown_and_foreign_themes_are_refused(self):
        self.add("classic")
        self.add("nope", expect=404)
        self.add("../x", expect=404)
        self.add("bold", name=" ", expect=400)
        missing = "00000000-0000-0000-0000-000000000000"
        self.ok("post", f"/api/v1/themes/library/{missing}/publish/", {}, expect=404)
        r = self.client.get("/api/v1/themes/settings/", HTTP_X_THEME_ID=missing)
        self.assertEqual(r.status_code, 404)  # never falls back to the live theme
        r = self.client.get("/api/v1/themes/settings/", HTTP_X_THEME_ID="not-a-uuid")
        self.assertEqual(r.status_code, 404)

        # Another store's theme can't be opened with this store's login
        other_token, other_store = self.sign_up("other-lib@example.com", "Other Store")
        theirs = self.ok("post", "/api/v1/themes/library/", {"source": "bold"}, expect=201)
        mine_token = self.token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {mine_token}", HTTP_X_STORE_ID=self.store["storeId"])
        r = self.client.get("/api/v1/themes/settings/", HTTP_X_THEME_ID=theirs["id"])
        self.assertEqual(r.status_code, 404)

    def test_a_store_has_a_limit_on_themes(self):
        from .services import MAX_THEMES

        for _ in range(MAX_THEMES):
            self.add("blank", name="t")
        self.add("blank", name="one too many", expect=400)


class StorefrontCustomerAuthAndAccountTest(StoreClientMixin, APITestCase):
    """Test theme-specific Login, Sign Up, and Customer Account pages."""

    def setUp(self):
        self.token, self.store = self.sign_up("owner-auth@example.com", "Auth Store")
        self.slug = self.store["storeSlug"]
        self.base = f"/s/{self.slug}"
        self.client.logout()

    def test_login_page_renders_cleanly(self):
        r = self.client.get(f"{self.base}/account/login")
        self.assertEqual(r.status_code, 200)
        content = r.content.decode()
        self.assertIn("Welcome back", content)
        self.assertIn(">Sign in</button>", content)
        self.assertIn('name="email"', content)
        self.assertIn('name="password"', content)
        self.assertIn(f"{self.base}/account/register", content)

    def test_login_and_register_styles_read_active_theme_without_changing_it(self):
        import copy
        import re

        from apps.themes.models import StoreTheme
        from apps.tenancy.models import Store
        from . import rendering
        from .services import get_store_theme

        store = Store.objects.get(store_id=self.store["storeId"])
        assignment = get_store_theme(store)  # finish normal first-request theme installation before taking the snapshot
        assignment = StoreTheme.objects.select_related("theme").get(pk=assignment.pk)
        theme = assignment.theme
        theme_data_before = copy.deepcopy(theme.theme_data)
        files_before = list(theme.files.order_by("file_path").values_list(
            "file_path", "content", "file_size", "updated_at",
        ))
        auth_css = str(rendering.CUSTOMER_AUTH_CSS)

        self.assertNotRegex(auth_css, r"#[0-9a-fA-F]{3,8}\b|rgba?\(")
        for theme_var in ("--text", "--accent", "--bg", "--line", "--muted", "--soft",
                          "--tss-body-font", "--tss-heading-font", "--b-r", "--b-bg", "--b-tx"):
            self.assertIn(f"var({theme_var}", auth_css)
        self.assertIn(":focus-visible", auth_css)
        self.assertIn("customer-card__header svg", auth_css)  # decorative lock badge is hidden

        for path in ("login", "register"):
            response = self.client.get(f"{self.base}/account/{path}")
            self.assertEqual(response.status_code, 200)
            html = response.content.decode()
            # The auth blocks keep their own styles (they are not stripped) and follow the theme
            self.assertIn(".auth-page .auth-tabs", html)
            self.assertIn("var(--tss-heading-font", html)
            self.assertIn("var(--accent", html)
            self.assertIn("data-auth-toggle", html)
            self.assertIn('aria-label="Show password"', html)
            # Both tabs are there on both pages
            self.assertIn(f'href="{self.base}/account/login', html)
            self.assertIn(f'href="{self.base}/account/register', html)
            self.assertIn(">Create account</a>", html)
            if path == "login":
                self.assertRegex(html, r'id="login-email" class="auth-input"')
                self.assertRegex(html, r'id="login-password" class="auth-input"')
                self.assertIn("New here?", html)
            else:
                for field in ("register-first-name", "register-last-name", "register-email",
                              "register-phone", "register-password", "register-confirm-password"):
                    self.assertRegex(html, rf'id="{field}" class="auth-input"')

        # Older themes with their own login markup still get the theme-variable cleanup
        legacy = str(rendering.customer_auth_content(
            '<style>.x{color:#fff}</style><a href="/s/x/account/register">Create account</a><h1>Hi</h1>', "login"))
        self.assertNotIn("#fff", legacy)
        self.assertNotIn("Create account</a>", legacy)
        self.assertIn(".auth-page.auth-page", legacy)

        theme.refresh_from_db()
        self.assertEqual(theme.theme_data, theme_data_before)
        self.assertEqual(list(theme.files.order_by("file_path").values_list(
            "file_path", "content", "file_size", "updated_at",
        )), files_before)

    def test_register_page_renders_cleanly(self):
        r = self.client.get(f"{self.base}/account/register")
        self.assertEqual(r.status_code, 200)
        content = r.content.decode()
        self.assertIn("Create Account", content)
        self.assertIn('name="first_name"', content)
        self.assertIn('name="last_name"', content)
        self.assertIn('name="email"', content)
        self.assertIn('name="password"', content)
        self.assertIn('name="confirm_password"', content)
        self.assertIn(f"{self.base}/account/login", content)

    def test_registration_creates_account_and_sets_cookie(self):
        from apps.customers.models import Customer
        from django.contrib.auth import get_user_model

        r = self.client.post(f"{self.base}/account/register", {
            "first_name": "Sarah",
            "last_name": "Connor",
            "email": "sarah.connor@example.com",
            "phone": "0400123456",
            "password": "Password123!",
            "confirm_password": "Password123!",
            "agree_terms": "true",
            "newsletter": "true",
        })
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.url.startswith(f"{self.base}/account?ct="))
        cookie_name = f"tss_cust_{self.slug}"
        self.assertIn(cookie_name, r.cookies)

        user = get_user_model().objects.filter(email="sarah.connor@example.com").first()
        self.assertIsNotNone(user)
        self.assertEqual(user.first_name, "Sarah")

        cust = Customer.objects.filter(email="sarah.connor@example.com").first()
        self.assertIsNotNone(cust)
        self.assertEqual(cust.email_subscription_status, "subscribed")

    def test_login_authenticates_customer(self):
        from django.contrib.auth import get_user_model
        get_user_model().objects.create_user(
            email="shopper@example.com",
            password="SecretPassword123!",
            first_name="John",
            last_name="Doe",
        )

        # Invalid password fails
        bad = self.client.post(f"{self.base}/account/login", {
            "email": "shopper@example.com",
            "password": "WrongPassword!",
        })
        self.assertEqual(bad.status_code, 400)
        self.assertIn("Invalid email or password", bad.content.decode())

        # Valid login succeeds
        ok = self.client.post(f"{self.base}/account/login", {
            "email": "shopper@example.com",
            "password": "SecretPassword123!",
        })
        self.assertEqual(ok.status_code, 302)
        self.assertTrue(ok.url.startswith(f"{self.base}/account?ct="))
        self.assertIn(f"tss_cust_{self.slug}", ok.cookies)

    def test_sign_in_works_without_the_cookie(self):
        """The storefront is sandboxed: the redirect's token, not the cookie, signs the shopper in."""
        self.client.post(f"{self.base}/account/register", {
            "first_name": "Sam", "last_name": "Cook", "email": "sam.cook@example.com",
            "password": "Password123!", "confirm_password": "Password123!", "agree_terms": "true",
        })
        self.client.cookies.clear()
        ok = self.client.post(f"{self.base}/account/login", {
            "email": "sam.cook@example.com", "password": "Password123!",
        })
        self.assertEqual(ok.status_code, 302)
        self.client.cookies.clear()  # what the sandboxed page really sends

        account = self.client.get(ok.url)
        self.assertEqual(account.status_code, 200)
        html = account.content.decode()
        self.assertIn("Welcome, Sam Cook", html)
        self.assertIn('input.name = "ct"', html)  # forms and links pass the token on

        # The profile and address forms post the token, not a cookie
        token = unquote(ok.url.split("ct=", 1)[1])
        saved = self.client.post(f"{self.base}/account/profile/", {
            "first_name": "Samuel", "last_name": "Cook", "phone": "", "ct": token,
        })
        self.assertEqual(saved.status_code, 302)
        self.assertIn("/account?notice=profile_saved&ct=", saved.url)
        self.assertIn("Welcome, Samuel Cook", self.client.get(saved.url).content.decode())

        # A wrong or foreign token does not sign anyone in
        self.client.cookies.clear()
        bad = self.client.get(f"{self.base}/account?ct=nonsense")
        self.assertIn(f"{self.base}/account/login", bad.url)

    def test_a_browser_that_dropped_the_login_cookie_stays_signed_in_on_its_next_visit(self):
        """The login form is a cross-site POST (the page is sandboxed), so a browser may refuse its cookie.
        The page the token opens gives the cookie again; a later visit without the token is still signed in."""
        self.client.post(f"{self.base}/account/register", {
            "first_name": "Ria", "last_name": "Shah", "email": "ria.shah@example.com",
            "password": "Password123!", "confirm_password": "Password123!", "agree_terms": "true",
        })
        self.client.cookies.clear()
        ok = self.client.post(f"{self.base}/account/login", {
            "email": "ria.shah@example.com", "password": "Password123!",
        })
        self.client.cookies.clear()  # the browser refused the cookie set on the POST response

        account = self.client.get(ok.url)  # the redirect: a top-level GET with ?ct=
        self.assertEqual(account.status_code, 200)
        cookie = account.cookies.get(f"tss_cust_{self.slug}")
        self.assertIsNotNone(cookie)
        self.assertEqual(cookie["path"], f"{self.base}/")
        self.assertTrue(cookie["httponly"])

        # Coming back later: no ?ct= in the address (the script removed it), only the cookie
        later = self.client.get(f"{self.base}/account")
        self.assertEqual(later.status_code, 200)
        self.assertIn("Welcome, Ria Shah", later.content.decode())

        # A shopper who already sends a valid cookie isn't given a new one on every page
        self.assertNotIn(f"tss_cust_{self.slug}", later.cookies)

    def test_the_customizer_previews_the_signed_in_account_page_with_sample_data(self):
        """The preview is the store owner's: the account page shows sample customer data, never the shopper
        signed in to the store in the same browser (the customizer sends that shopper to the real store)."""
        self.client.post(f"{self.base}/account/register", {
            "first_name": "Ria", "last_name": "Shah", "email": "ria.preview@example.com",
            "password": "Password123!", "confirm_password": "Password123!", "agree_terms": "true",
        })
        self.assertIn(f"tss_cust_{self.slug}", self.client.cookies)

        self.use(self.token, self.store["storeId"])
        data = self.ok("get", "/api/v1/themes/settings/")
        html = self.ok("post", "/api/v1/themes/preview/?customer=account", data)["html"]
        self.assertIn("customer-account-section", html)
        self.assertIn("Jane Doe", html)
        self.assertNotIn("Ria", html)

        # Other values still show the page being edited
        home = self.ok("post", "/api/v1/themes/preview/?customer=orders", data)["html"]
        self.assertNotIn("customer-account-section", home)

    def test_address_double_save_delete_and_customer_sync(self):
        from apps.customers.models import Customer, CustomerAddress
        self.client.post(f"{self.base}/account/register", {
            "first_name": "Dee", "last_name": "Lete", "email": "dee@example.com",
            "password": "Password123!", "confirm_password": "Password123!", "agree_terms": "true",
        })
        address = {
            "action": "add", "first_name": "Dee", "last_name": "Lete", "address_line_1": "1 Main St",
            "city": "Pune", "state": "MH", "postal_code": "411001", "country": "India",
        }
        first = self.client.post(f"{self.base}/account/addresses/", address)
        self.assertIn("notice=address_added", first.url)
        # The same address saved again (a double click, any letter case) is not added twice
        again = self.client.post(f"{self.base}/account/addresses/", {**address, "address_line_1": "1 MAIN ST "})
        self.assertIn("notice=address_exists", again.url)
        customer = Customer.objects.get(email="dee@example.com")
        self.assertEqual(CustomerAddress.objects.filter(customer=customer).count(), 1)

        # The customer row (what the admin Customers list shows) follows the default address
        customer.refresh_from_db()
        self.assertEqual((customer.city, customer.state, customer.country), ("Pune", "MH", "India"))

        page = self.client.get(f"{self.base}/account/?notice=address_exists").content.decode()
        self.assertIn("already in your address book", page)
        self.assertIn("data-acct-ask", page)  # delete asks inline
        self.assertNotIn("onsubmit=\"return confirm", page)  # the sandbox blocks the browser's confirm box

        stored = CustomerAddress.objects.get(customer=customer)
        gone = self.client.post(f"{self.base}/account/addresses/", {"action": "delete", "address_id": str(stored.pk)})
        self.assertIn("notice=address_deleted", gone.url)
        self.assertFalse(CustomerAddress.objects.filter(customer=customer).exists())
        customer.refresh_from_db()
        self.assertIsNone(customer.city)

    def test_account_page_requires_auth(self):
        # Unauthenticated redirects to login
        r = self.client.get(f"{self.base}/account/")
        self.assertEqual(r.status_code, 302)
        self.assertIn(f"{self.base}/account/login", r.url)

    def test_authenticated_account_page_and_address_management(self):
        # 1. Register customer
        self.client.post(f"{self.base}/account/register", {
            "first_name": "Alex",
            "last_name": "Rider",
            "email": "alex.rider@example.com",
            "password": "Password123!",
            "confirm_password": "Password123!",
            "agree_terms": "true",
        })

        # 2. Open account page
        acc = self.client.get(f"{self.base}/account/")
        self.assertEqual(acc.status_code, 200)
        content = acc.content.decode()
        self.assertIn("Welcome, Alex Rider", content)
        self.assertIn("alex.rider@example.com", content)
        self.assertIn("Order history", content)
        self.assertIn("Saved addresses", content)

        # 3. Add an address
        add_res = self.client.post(f"{self.base}/account/addresses/", {
            "action": "add",
            "first_name": "Alex",
            "last_name": "Rider",
            "address_line_1": "10 Downing St",
            "city": "London",
            "state": "Greater London",
            "postal_code": "SW1A 2AA",
            "country": "United Kingdom",
            "is_default": "true",
        })
        self.assertEqual(add_res.status_code, 302)

        # Check address rendered
        acc_after = self.client.get(f"{self.base}/account/")
        self.assertEqual(acc_after.status_code, 200)
        self.assertIn("10 Downing St", acc_after.content.decode())
        self.assertIn("Default", acc_after.content.decode())

        # 4. Sign out
        out = self.client.get(f"{self.base}/account/logout")
        self.assertEqual(out.status_code, 302)

        # 5. Accessing account now redirects to login
        acc_logged_out = self.client.get(f"{self.base}/account/")
        self.assertEqual(acc_logged_out.status_code, 302)
        self.assertIn(f"{self.base}/account/login", acc_logged_out.url)

    def test_all_preset_themes_contain_customer_styles_and_build(self):
        from .presets import build, list_presets
        presets = list_presets()
        for preset in presets:
            slug = preset["slug"]
            files, _, _ = build(slug)
            self.assertIn("templates/login.liquid", files)
            self.assertIn("templates/register.liquid", files)
            self.assertIn("templates/account.liquid", files)
            self.assertIn("snippets/customer-auth.liquid", files)
            self.assertIn("snippets/customer-account.liquid", files)
            if slug != "classic":
                theme_styles = files.get("snippets/theme-styles.liquid", "")
                self.assertIn(".customer-auth", theme_styles)
                self.assertIn(".customer-account", theme_styles)

    def test_storefront_header_account_navigation_logged_out_and_logged_in(self):
        # 1. Logged-out shopper: Account icon links to /account/login
        self.client.cookies.clear()
        r = self.client.get(f"{self.base}/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn('class="site-header__search"', html)
        header = html[html.index('<header class="site-header"'):html.index("</header>")]
        self.assertEqual(header.count('class="site-header__search"'), 1)
        self.assertNotIn('href="' + self.base + '/collections/all" aria-label="Search"', header)
        self.assertIn('data-header-search', header)
        self.assertIn('aria-expanded="false"', header)
        self.assertIn(f'action="{self.base}/collections/all" method="get" role="search"', header)
        self.assertIn('name="q"', header)
        self.assertIn("requestAnimationFrame", header)
        self.assertIn("input.focus()", header)
        self.assertIn("!root.contains(event.target)", header)
        self.assertIn("event.key === 'Escape'", header)
        self.assertIn(f'class="site-header__account" href="{self.base}/account/login"', html)
        self.assertIn('aria-label="Sign in or create account"', html)
        self.assertIn('class="site-header__wishlist"', html)
        self.assertIn('class="site-header__cart"', html)

        # 2. Visiting the destination page: the login form, with a link to create an account
        r_auth = self.client.get(f"{self.base}/account/login")
        self.assertEqual(r_auth.status_code, 200)
        auth_html = r_auth.content.decode()
        self.assertIn('<h1 class="auth-heading__title">Welcome back</h1>', auth_html)
        self.assertIn('>Sign in</button>', auth_html)
        self.assertIn('Create an account', auth_html)
        self.assertIn(f'{self.base}/account/login', auth_html)
        self.assertIn(f'{self.base}/account/register', auth_html)

        # 3. Register & log in customer
        self.client.post(f"{self.base}/account/register", {
            "first_name": "Oliver",
            "last_name": "Queen",
            "email": "oliver@example.com",
            "password": "Password123!",
            "confirm_password": "Password123!",
            "agree_terms": "true",
        })

        # 4. Logged-in shopper: Account icon links to /account/
        r_logged_in = self.client.get(f"{self.base}/")
        self.assertEqual(r_logged_in.status_code, 200)
        html_logged_in = r_logged_in.content.decode()
        self.assertIn(f'class="site-header__account" href="{self.base}/account/"', html_logged_in)
        self.assertIn('aria-label="Account"', html_logged_in)

        # 5. Visiting /account/ when logged in: renders Account/Profile page
        r_acc = self.client.get(f"{self.base}/account/")
        self.assertEqual(r_acc.status_code, 200)
        self.assertIn('My Account', r_acc.content.decode())

    def add_theme(self, source="blank"):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.token}", HTTP_X_STORE_ID=self.store["storeId"])
        theme = self.ok("post", "/api/v1/themes/library/", {"source": source}, expect=201)
        self.client.credentials()
        return theme

    def test_storefront_header_across_all_preset_themes(self):
        from .presets import list_presets
        from .views import set_storefront_customer_cookie
        from django.contrib.auth import get_user_model
        from django.http import HttpResponse

        user, _ = get_user_model().objects.get_or_create(
            email="theme-cust@example.com",
            defaults={"first_name": "Bruce", "last_name": "Wayne"},
        )
        from apps.tenancy.models import Store
        store_obj = Store.objects.get(pk=self.store["storeId"])
        dummy = HttpResponse()
        set_storefront_customer_cookie(dummy, store_obj, user)
        cookie_val = dummy.cookies[f"tss_cust_{self.slug}"].value

        presets = list_presets()
        for preset in presets:
            slug = preset["slug"]
            theme = self.add_theme(slug)
            self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.token}", HTTP_X_STORE_ID=self.store["storeId"])
            self.client.post(f"/api/v1/themes/library/{theme['id']}/publish/")
            self.client.credentials()

            # Logged out: header icon points to /account/login with "Sign in or create account"
            self.client.cookies.clear()
            r_out = self.client.get(f"{self.base}/")
            self.assertEqual(r_out.status_code, 200)
            html_out = r_out.content.decode()
            self.assertIn('class="site-header__search"', html_out)
            self.assertIn(f'class="site-header__account" href="{self.base}/account/login"', html_out)
            self.assertIn('aria-label="Sign in or create account"', html_out)

            # Destination page renders the login form
            r_dest = self.client.get(f"{self.base}/account/login")
            self.assertEqual(r_dest.status_code, 200)
            dest_html = r_dest.content.decode()
            self.assertIn('<h1 class="auth-heading__title">Welcome back</h1>', dest_html)
            self.assertIn('>Sign in</button>', dest_html)
            self.assertIn('Create an account', dest_html)

            # Logged in: header icon points to /account/
            self.client.cookies[f"tss_cust_{self.slug}"] = cookie_val
            r_in = self.client.get(f"{self.base}/")
            self.assertEqual(r_in.status_code, 200)
            html_in = r_in.content.decode()
            self.assertIn(f'class="site-header__account" href="{self.base}/account/"', html_in)
            self.assertIn('aria-label="Account"', html_in)

            # Destination account page renders correctly
            r_in_acc = self.client.get(f"{self.base}/account/")
            self.assertEqual(r_in_acc.status_code, 200)
            self.assertIn('My Account', r_in_acc.content.decode())

            self.client.cookies.clear()


class StorefrontThemeCheckoutTest(StoreClientMixin, APITestCase):
    """Test independent theme-specific checkout pages, persistence, and functionality."""

    def setUp(self):
        from rest_framework.test import APIClient
        self.token, self.store = self.sign_up("owner-checkout@example.com", "Checkout Test Store")
        self.slug = self.store["storeSlug"]
        self.base = f"/s/{self.slug}"
        self.location = self.ok("post", "/api/v1/inventory/locations/", {"name": "Main"}, expect=201)["id"]
        self.product = self.ok("post", "/api/v1/catalog/products/", {
            "title": "Minimalist Lamp", "sku": "LAMP-1", "status": "active", "price": "49.99",
            "location": self.location, "quantity": 10,
        }, expect=201)
        self.shopper = APIClient()

    def add_theme(self, source="blank", name=None):
        self.use(self.token, self.store["storeId"])
        payload = {"source": source}
        if name:
            payload["name"] = name
        return self.ok("post", "/api/v1/themes/library/", payload, expect=201)

    def editing(self, theme_id, method, url, data=None):
        kwargs = {"HTTP_X_THEME_ID": theme_id}
        r = getattr(self.client, method)(url, data, format="json", **kwargs) if data is not None \
            else getattr(self.client, method)(url, **kwargs)
        self.assertEqual(r.status_code, 200, r.content[:300])
        return r.json()

    def code(self, theme_id, path):
        files = self.editing(theme_id, "get", "/api/v1/themes/files/")["files"]
        return next((f["content"] for f in files if f["path"] == path), None)

    def save_code(self, theme_id, path, content):
        return self.editing(theme_id, "post", "/api/v1/themes/files/", {"path": path, "content": content})

    def test_all_themes_start_with_default_checkout_templates_and_sections(self):
        # 1. First theme added is published and starts with default checkout template and main-checkout section
        active_theme = self.add_theme(source="blank", name="Initial Theme")

        checkout_content = self.code(active_theme["id"], "templates/checkout.liquid")
        self.assertIsNotNone(checkout_content)
        self.assertIn("sections", checkout_content)

        main_checkout_content = self.code(active_theme["id"], "sections/main-checkout.liquid")
        self.assertIsNotNone(main_checkout_content)
        self.assertIn("checkout-form", main_checkout_content)
        self.assertIn("checkout-summary", main_checkout_content)

        # 2. Add themes from different presets (bold, boutique) and verify identical default checkout files
        theme_bold = self.add_theme(source="bold", name="Bold Theme")
        theme_boutique = self.add_theme(source="boutique", name="Boutique Theme")

        for th in (theme_bold, theme_boutique):
            th_checkout = self.code(th["id"], "templates/checkout.liquid")
            th_main_sec = self.code(th["id"], "sections/main-checkout.liquid")
            self.assertEqual(th_checkout, checkout_content)
            self.assertEqual(th_main_sec, main_checkout_content)

    def test_independent_checkout_editing_per_theme_no_cross_changes(self):
        theme_a = self.add_theme(source="blank", name="Theme A")
        theme_b = self.add_theme(source="bold", name="Theme B")

        # 1. Modify Theme A's checkout page
        theme_a_code = "{% comment %}Theme A Custom Checkout{% endcomment %}\n<div class='theme-a-checkout'>{{ sections }}</div>"
        self.save_code(theme_a["id"], "templates/checkout.liquid", theme_a_code)

        # 2. Verify Theme A has the change
        self.assertEqual(self.code(theme_a["id"], "templates/checkout.liquid"), theme_a_code)

        # 3. Verify Theme B is untouched
        theme_b_code_before = self.code(theme_b["id"], "templates/checkout.liquid")
        self.assertNotIn("theme-a-checkout", theme_b_code_before)
        self.assertIn("sections", theme_b_code_before)

        # 4. Modify Theme B's checkout page
        theme_b_code = "{% comment %}Theme B Custom Checkout{% endcomment %}\n<div class='theme-b-checkout'>{{ sections }}</div>"
        self.save_code(theme_b["id"], "templates/checkout.liquid", theme_b_code)

        # 5. Verify both retain their independent customizations
        self.assertEqual(self.code(theme_a["id"], "templates/checkout.liquid"), theme_a_code)
        self.assertEqual(self.code(theme_b["id"], "templates/checkout.liquid"), theme_b_code)

    def test_checkout_customization_persists_across_theme_switches_and_renders(self):
        theme_a = self.add_theme(source="blank", name="Theme A")
        theme_b = self.add_theme(source="bold", name="Theme B")

        # Customize checkout section for Theme A
        custom_a_section = "<div class='theme-a-sec'><h1>Theme A Checkout</h1></div>"
        self.save_code(theme_a["id"], "sections/main-checkout.liquid", custom_a_section)

        # Customize checkout section for Theme B
        custom_b_section = "<div class='theme-b-sec'><h1>Theme B Checkout</h1></div>"
        self.save_code(theme_b["id"], "sections/main-checkout.liquid", custom_b_section)

        # Publish Theme A -> storefront /checkout renders Theme A
        self.ok("post", f"/api/v1/themes/library/{theme_a['id']}/publish/", {})
        r_a = self.shopper.get(f"{self.base}/checkout")
        self.assertEqual(r_a.status_code, 200)
        self.assertIn("theme-a-sec", r_a.content.decode())
        self.assertNotIn("theme-b-sec", r_a.content.decode())

        # Publish Theme B -> storefront /checkout renders Theme B
        self.ok("post", f"/api/v1/themes/library/{theme_b['id']}/publish/", {})
        r_b = self.shopper.get(f"{self.base}/checkout")
        self.assertEqual(r_b.status_code, 200)
        self.assertIn("theme-b-sec", r_b.content.decode())
        self.assertNotIn("theme-a-sec", r_b.content.decode())

        # Re-publish Theme A -> still retains Theme A customizations
        self.ok("post", f"/api/v1/themes/library/{theme_a['id']}/publish/", {})
        r_a2 = self.shopper.get(f"{self.base}/checkout")
        self.assertEqual(r_a2.status_code, 200)
        self.assertIn("theme-a-sec", r_a2.content.decode())
        self.assertNotIn("theme-b-sec", r_a2.content.decode())

    def test_checkout_rejects_email_without_top_level_domain_for_every_payment_method(self):
        import re
        from apps.orders.models import Order
        self.add_theme(source="blank", name="Main Theme")
        home = self.shopper.get(f"{self.base}/")
        session_id = re.search(r'data-cart-session="([0-9a-f]{32})"', home.content.decode()).group(1)
        self.shopper.post(
            f"{self.base}/cart/add", data=json.dumps({"product": self.product["id"], "quantity": 1}),
            content_type="application/json", HTTP_X_CART_SESSION=session_id,
        )
        form = {
            "first_name": "Jav", "last_name": "V", "address1": "1 Main Street", "city": "Pune",
            "postal_code": "411001", "country": "India",
        }
        for method in ("cod", "online_demo", "cash_on_delivery", ""):
            for bad in ("jv@gmail", "jav", "", "  ", "jv@gmail.c", "a@b.com\r\nBcc: x@y.com"):
                with self.subTest(method=method, email=bad):
                    r = self.shopper.post(
                        f"{self.base}/checkout/", data=json.dumps({**form, "email": bad, "payment_method": method}),
                        content_type="application/json", HTTP_ACCEPT="application/json", HTTP_X_CART_SESSION=session_id,
                    )
                    self.assertEqual(r.status_code, 400, r.content[:200])
                    self.assertIn(r.json()["error"], ("Enter an email address", "Enter a valid email address"))
                    self.assertEqual(r.json()["field"], "email")
                    self.assertEqual(Order.objects.count(), 0)

    def _checkout_session(self):
        import re
        self.add_theme(source="blank", name="Main Theme")
        home = self.shopper.get(f"{self.base}/")
        session_id = re.search(r'data-cart-session="([0-9a-f]{32})"', home.content.decode()).group(1)
        self.shopper.post(
            f"{self.base}/cart/add", data=json.dumps({"product": self.product["id"], "quantity": 1}),
            content_type="application/json", HTTP_X_CART_SESSION=session_id,
        )
        return session_id

    def _post_checkout(self, session_id, **extra):
        form = {
            "email": "jv@example.com", "first_name": "Jav", "last_name": "V", "address1": "1 Main Street",
            "city": "Pune", "postal_code": "411001", "country": "India", "payment_method": "cod", **extra,
        }
        return self.shopper.post(
            f"{self.base}/checkout/", data=json.dumps(form), content_type="application/json",
            HTTP_ACCEPT="application/json", HTTP_X_CART_SESSION=session_id,
        )

    def test_checkout_settings_make_phone_company_and_address2_required(self):
        from apps.orders.models import Order
        from apps.tenancy.models import Store
        session_id = self._checkout_session()
        Store.objects.filter(store_slug=self.store["storeSlug"]).update(
            checkout_phone="required", checkout_company="required", checkout_address2="required")
        for message, extra in (
            ("Company name is required.", {}),
            ("Apartment, suite or unit is required.", {"company": "Acme"}),
            ("Phone number is required.", {"company": "Acme", "address2": "Flat 2"}),
        ):
            r = self._post_checkout(session_id, **extra)
            self.assertEqual((r.status_code, r.json()["error"]), (400, message))
        self.assertEqual(Order.objects.count(), 0)
        r = self._post_checkout(session_id, company="Acme", address2="Flat 2", phone="9876543210")
        self.assertEqual(r.status_code, 200, r.content[:200])

    def test_checkout_last_name_only_does_not_need_a_first_name(self):
        from apps.tenancy.models import Store
        session_id = self._checkout_session()
        r = self._post_checkout(session_id, first_name="")
        self.assertEqual(r.status_code, 400)
        Store.objects.filter(store_slug=self.store["storeSlug"]).update(checkout_full_name="last_only")
        r = self._post_checkout(session_id, first_name="")
        self.assertEqual(r.status_code, 200, r.content[:200])

    def test_checkout_can_require_sign_in(self):
        from apps.tenancy.models import Store
        session_id = self._checkout_session()
        Store.objects.filter(store_slug=self.store["storeSlug"]).update(checkout_require_login=True)
        page = self.shopper.get(f"{self.base}/checkout")
        self.assertEqual(page.status_code, 302)
        self.assertIn("/account/login", page["Location"])
        self.assertEqual(self._post_checkout(session_id).status_code, 403)

    def test_checkout_refuses_to_place_order_with_bad_email_already_saved_on_it(self):
        import re
        from apps.commerce.models import Checkout
        from apps.commerce import services as commerce_services
        from apps.orders.models import Order
        self.add_theme(source="blank", name="Main Theme")
        home = self.shopper.get(f"{self.base}/")
        session_id = re.search(r'data-cart-session="([0-9a-f]{32})"', home.content.decode()).group(1)
        self.shopper.post(
            f"{self.base}/cart/add", data=json.dumps({"product": self.product["id"], "quantity": 1}),
            content_type="application/json", HTTP_X_CART_SESSION=session_id,
        )
        self.shopper.post(f"{self.base}/checkout/", data=json.dumps({"action": "apply_discount", "discount_code": ""}),
                          content_type="application/json", HTTP_X_CART_SESSION=session_id)
        checkout = Checkout.objects.get()
        Checkout.objects.filter(pk=checkout.pk).update(email="jv@gmail", shipping_address={"first_name": "J", "address1": "1 St"})
        checkout.refresh_from_db()
        for method in ("cod", "online_demo"):
            with self.assertRaises(Exception):
                commerce_services._place_order(checkout, method)
        self.assertEqual(Order.objects.count(), 0)

    def test_storefront_checkout_full_order_journey(self):
        import re
        self.add_theme(source="blank", name="Main Theme")

        # Shopper visits home to obtain cart session
        home = self.shopper.get(f"{self.base}/")
        match = re.search(r'data-cart-session="([0-9a-f]{32})"', home.content.decode())
        self.assertIsNotNone(match)
        session_id = match.group(1)

        # Add item to cart
        add_resp = self.shopper.post(
            f"{self.base}/cart/add",
            data=json.dumps({"product": self.product["id"], "quantity": 2}),
            content_type="application/json",
            HTTP_X_CART_SESSION=session_id
        )
        self.assertEqual(add_resp.status_code, 200)

        # View checkout page
        checkout_page = self.shopper.get(f"{self.base}/checkout?cart={session_id}")
        self.assertEqual(checkout_page.status_code, 200)
        html = checkout_page.content.decode()
        self.assertIn("Minimalist Lamp", html)
        self.assertIn("Contact information", html)
        self.assertIn("Delivery address", html)
        self.assertIn("Cash on Delivery (COD)", html)

        # Submit order via checkout form
        order_post = self.shopper.post(
            f"{self.base}/checkout?cart={session_id}",
            data={
                "email": "eleanor@example.com",
                "first_name": "Eleanor",
                "last_name": "Vance",
                "phone": "555-0199",
                "address_line_1": "42 Hill House Lane",
                "city": "Boston",
                "state": "MA",
                "postal_code": "02108",
                "country": "United States",
                "payment_method": "cash_on_delivery",
                "order_notes": "Leave at front door",
            }
        )
        self.assertEqual(order_post.status_code, 302)
        redirect_url = order_post.url
        self.assertIn(f"{self.base}/checkout/order/", redirect_url)

        # Order confirmation page
        conf_page = self.shopper.get(redirect_url)
        self.assertEqual(conf_page.status_code, 200)
        conf_html = conf_page.content.decode()
        self.assertIn("Thank you for your order!", conf_html)
        self.assertIn("Eleanor Vance", conf_html)
        self.assertIn("42 Hill House Lane", conf_html)
        self.assertIn("Minimalist Lamp", conf_html)

        # Verify Order object in database
        from apps.orders.models import Order
        order = Order.objects.filter(customer__email="eleanor@example.com").first()
        self.assertIsNotNone(order)
        self.assertEqual(order.items.count(), 1)
        self.assertEqual(order.items.first().product.title, "Minimalist Lamp")
        self.assertEqual(order.items.first().quantity, 2)

    def test_checkout_renders_stock_error_if_stock_runs_out_before_order_placement(self):
        import re
        from unittest import mock

        from django.db.models import F

        from apps.commerce import services as commerce_services
        from apps.inventory.models import InventoryItem

        self.add_theme(source="blank", name="Main Theme")
        home = self.shopper.get(f"{self.base}/")
        session_id = re.search(r'data-cart-session="([0-9a-f]{32})"', home.content.decode()).group(1)
        added = self.shopper.post(
            f"{self.base}/cart/add",
            data=json.dumps({"product": self.product["id"], "quantity": 2}),
            content_type="application/json",
            HTTP_X_CART_SESSION=session_id,
        )
        self.assertEqual(added.status_code, 200, added.content[:300])

        place_order = commerce_services.place_order

        def run_out_then_place(checkout, payment_method="cod"):
            InventoryItem.objects.filter(variant_id=self.product["variants"][0]["id"]).update(
                available=1, on_hand=1 + F("committed") + F("unavailable"))
            return place_order(checkout, payment_method=payment_method)

        with mock.patch("apps.themes.views.commerce_services.place_order", side_effect=run_out_then_place):
            response = self.shopper.post(
                f"{self.base}/checkout?cart={session_id}",
                data={
                    "email": "buyer@example.com", "first_name": "Buyer", "last_name": "One",
                    "address1": "1 Main St", "city": "Pune", "postal_code": "411001",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Only 1 of Minimalist Lamp left", response.content.decode())

    def test_sandboxed_checkout_json_places_order(self):
        """The sandboxed page (origin null) preflights, then POSTs JSON: both must work and the order must exist."""
        import re
        self.add_theme(source="blank", name="Main Theme")
        home = self.shopper.get(f"{self.base}/")
        session_id = re.search(r'data-cart-session="([0-9a-f]{32})"', home.content.decode()).group(1)
        self.shopper.post(
            f"{self.base}/cart/add", data=json.dumps({"product": self.product["id"], "quantity": 1}),
            content_type="application/json", HTTP_X_CART_SESSION=session_id,
        )

        preflight = self.shopper.options(
            f"{self.base}/checkout/", HTTP_ORIGIN="null",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="content-type,x-cart-session",
        )
        self.assertEqual(preflight.status_code, 204)
        self.assertEqual(preflight["Access-Control-Allow-Origin"], "*")
        self.assertIn("X-Cart-Session", preflight["Access-Control-Allow-Headers"])

        r = self.shopper.post(
            f"{self.base}/checkout/", data=json.dumps({
                "email": "sam@example.com", "first_name": "Sam", "last_name": "Lee",
                "address1": "1 Main St", "city": "Pune", "postal_code": "411001", "payment_method": "cod",
            }),
            content_type="application/json", HTTP_ACCEPT="application/json",
            HTTP_ORIGIN="null", HTTP_X_CART_SESSION=session_id,
        )
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertEqual(r["Access-Control-Allow-Origin"], "*")
        self.assertTrue(r.json()["success"])
        from apps.orders.models import Order
        self.assertTrue(Order.objects.filter(customer__email="sam@example.com").exists())

    def test_cart_drawer_checkout_button_navigation_and_rendering(self):
        """Test complete flow: add product -> cart drawer -> checkout button -> checkout page rendering."""
        import re
        self.add_theme(source="blank", name="Active Store Theme")

        # 1. Shopper visits storefront to initialize cart session
        home_resp = self.shopper.get(f"{self.base}/")
        self.assertEqual(home_resp.status_code, 200)
        session_match = re.search(r'data-cart-session="([0-9a-f]{32})"', home_resp.content.decode())
        self.assertIsNotNone(session_match)
        session_id = session_match.group(1)

        # 2. Add product to cart
        add_resp = self.shopper.post(
            f"{self.base}/cart/add",
            data=json.dumps({"product": self.product["id"], "quantity": 3}),
            content_type="application/json",
            HTTP_X_CART_SESSION=session_id
        )
        self.assertEqual(add_resp.status_code, 200)

        # 3. Request cart fragment (the content inside the cart drawer)
        frag_resp = self.shopper.get(
            f"{self.base}/cart/fragment",
            HTTP_X_CART_SESSION=session_id
        )
        self.assertEqual(frag_resp.status_code, 200)
        frag_html = frag_resp.json().get("html", "")
        # The drawer includes the checkout button
        self.assertIn("cart-checkout", frag_html)
        self.assertIn("Check out", frag_html)

        # 4. Navigate to checkout URL (both with query param and with session cookie)
        checkout_resp = self.shopper.get(f"{self.base}/checkout?cart={session_id}")
        self.assertEqual(checkout_resp.status_code, 200)
        checkout_html = checkout_resp.content.decode()

        # 5. Verify checkout page renders correctly:
        # - Has the selected theme's checkout template structure
        self.assertIn("checkout-page", checkout_html)
        self.assertIn("checkout-container", checkout_html)
        # - Cart item details preserved
        self.assertIn("Minimalist Lamp", checkout_html)
        self.assertIn("3", checkout_html)  # Quantity badge
        # - Checkout sections connected
        self.assertIn("Delivery address", checkout_html)
        self.assertIn("Contact information", checkout_html)
        self.assertIn("Payment method", checkout_html)
        self.assertIn("Cash on Delivery (COD)", checkout_html)
        self.assertIn("Place order", checkout_html)



class StorefrontBlocksTest(StoreClientMixin, APITestCase):
    """Newsletter, notice box, quiz, utility bar, search, badges and the product page's tier cards and zoom."""

    def setUp(self):
        self.token, self.store = self.sign_up("blocks@example.com", "Blocks Store")
        self.base = f"/s/{self.store['storeSlug']}"
        self.mug = self.ok("post", "/api/v1/catalog/products/", {
            "title": "Blue mug", "sku": "SKU-MUG", "status": "active", "price": "12.50",
        }, expect=201)
        self.ok("post", "/api/v1/catalog/products/", {
            "title": "Red scarf", "sku": "SKU-SCARF", "status": "active", "price": "30.00",
        }, expect=201)

    def add_sections(self, page, sections):
        data = self.ok("get", "/api/v1/themes/settings/")
        template = data["templates"][page]
        template["sections"].update(sections)
        self.ok("patch", "/api/v1/themes/settings/", {"templates": {page: {
            "order": template["order"] + list(sections), "sections": template["sections"]}}})

    def test_newsletter_and_notice_render_on_the_home_page(self):
        self.add_sections("index", {
            "news": {"type": "newsletter", "settings": {"heading": "Stay in touch"}},
            "notice": {"type": "static-text", "settings": {"heading": "Please drink responsibly"}},
        })
        html = self.client.get(f"{self.base}/").content.decode()
        self.assertIn("Stay in touch", html)
        self.assertIn(f'action="{self.base}/newsletter"', html)
        self.assertIn("Please drink responsibly", html)
        self.assertIn('role="note"', html)

    def test_a_removed_section_type_in_saved_data_is_skipped_quietly(self):
        from .models import StoreTheme

        self.add_sections("index", {"news": {"type": "newsletter", "settings": {"heading": "Stay in touch"}}})
        assignment = StoreTheme.objects.get(store__store_id=self.store["storeId"], is_active=True)
        index = assignment.custom_data["templates"]["index"]
        index["sections"]["old"] = {"type": "quiz", "settings": {"heading": "Old quiz heading"}}
        index["order"].append("old")
        assignment.save(update_fields=["custom_data"])

        r = self.client.get(f"{self.base}/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Stay in touch", r.content.decode())
        self.assertNotIn("Old quiz heading", r.content.decode())
        settings = self.ok("get", "/api/v1/themes/settings/")
        self.assertNotIn("old", settings["templates"]["index"]["sections"])
        self.assertNotIn("old", settings["templates"]["index"]["order"])
        self.ok("post", "/api/v1/themes/preview/", settings)  # the customizer preview does not crash either
        self.ok("patch", "/api/v1/themes/settings/", {"templates": {"index": {
            "order": index["order"], "sections": index["sections"]}}})  # saving still works with the old section in it
        self.assertNotIn("old", self.ok("get", "/api/v1/themes/settings/")["templates"]["index"]["sections"])

    MATCH_BLOCKS = [
        {"type": "question", "settings": {
            "question": "Pick a mood", "answer_1_label": "Crisp", "answer_1_icon": "leaf", "answer_1_key": "Cider",
            "answer_2_label": "Rich", "answer_2_icon": "moon", "answer_2_key": "stout"}},
        {"type": "question", "settings": {"question": "Pick a time", "answer_1_label": "Day", "answer_1_key": "cider"}},
        {"type": "result", "settings": {"result_key": "stout", "name": "Stout", "link": "/collections/stout"}},
        {"type": "result", "settings": {"result_key": "cider", "name": "Cider", "text": "Apple joy", "button_label": ""}},
    ]

    def test_match_quiz_renders_on_the_home_page_with_blocks_and_anchor(self):
        self.add_sections("index", {"match": {"type": "match-quiz", "settings": {"heading": "Find your match"},
                                              "blocks": self.MATCH_BLOCKS}})
        html = self.client.get(f"{self.base}/").content.decode()
        self.assertIn('id="expert-match-quiz"', html)  # the default section ID
        self.assertIn("data-mq-open", html)
        self.assertIn("Question 1 of 2", html)
        self.assertIn("Question 2 of 2", html)
        self.assertIn('data-key="cider"', html)  # result keys are matched in lower case
        self.assertIn("Pick a mood", html)
        self.assertIn(f'data-link="{self.base}/collections/stout"', html)
        self.assertIn('data-label="Shop now"', html)  # empty button text falls back to the default
        self.assertEqual(html.count('class="mq-modal__bar"'), 2)

    def test_match_quiz_result_logic_is_in_the_section_file(self):
        code = self.ok("get", "/api/v1/themes/files/")["files"]
        script = next(f["content"] for f in code if f["path"] == "sections/match-quiz.liquid")
        # most picks wins; the first Result block wins a tie and is the fallback
        self.assertIn("count > best", script)
        self.assertIn("var winner = results[0]", script)

    def test_match_quiz_new_section_defaults(self):
        from .schemas import SECTIONS

        spec = SECTIONS["match-quiz"]
        settings = {key: option["default"] for key, option in spec["settings"].items()}
        self.assertEqual(settings["result_logic"], "priority")
        self.assertEqual(settings["question_priority"], "4,3,1,2")
        self.assertEqual(settings["anchor_id"], "expert-match-quiz")
        self.assertEqual(settings["button_label"], "FIND YOUR MATCH")
        self.assertEqual(settings["heading"], "Can't decide? Let our Sommelier guide you.")
        self.assertEqual((settings["panel_color"], settings["panel_text_color"]), ("#fff6e3", "#24151a"))
        self.assertEqual((settings["accent_color"], settings["popup_color"]), ("#6b2737", "#fffaf2"))
        # a new section starts with no Question and no Result blocks: each owner adds their own
        self.assertNotIn("default_blocks", spec)
        self.add_sections("index", {"match": {"type": "match-quiz", "settings": {}}})
        html = self.client.get(f"{self.base}/").content.decode()
        self.assertIn('id="expert-match-quiz"', html)
        self.assertNotIn('<button class="match-quiz__button" type="button" data-mq-open', html)  # the element, not CSS/JS text
        self.assertNotIn('class="mq-modal__bar"', html)

    PRIORITY_BLOCKS = [
        {"type": "question", "settings": {"question": "Q1", "answer_1_label": "One", "answer_1_result_name": "From one",
                                          "answer_2_label": "Two"}},
        {"type": "question", "settings": {"question": "Q2", "answer_1_label": "Three"}},
        {"type": "question", "settings": {"question": "Q3", "answer_1_label": "Four", "answer_1_link": "stout-shelf"}},
        {"type": "question", "settings": {
            "question": "Q4", "answer_1_label": "Light", "answer_1_result_name": "Beer & Cider",
            "answer_1_result_text": "Fresh beers.", "answer_1_link": "/collections/beer",
            "answer_2_label": "Strong", "answer_2_link": "stout-shelf"}},
    ]

    def test_match_quiz_priority_answers_carry_their_own_result(self):
        self.ok("post", "/api/v1/catalog/collections/", {"name": "Stout shelf", "status": "active"}, expect=201)
        self.add_sections("index", {"match": {"type": "match-quiz", "settings": {}, "blocks": self.PRIORITY_BLOCKS}})
        html = self.client.get(f"{self.base}/").content.decode()
        self.assertIn('data-mode="priority"', html)
        self.assertNotIn("data-mq-result ", html)  # no Result blocks needed
        # an answer with a name: its name, text and the collection link
        self.assertIn(
            f'data-rname="Beer &amp; Cider" data-rtext="Fresh beers." data-rlink="{self.base}/collections/beer"', html)
        # no name: the collection's title is the result name
        self.assertIn(f'data-rname="Stout shelf" data-rtext="" data-rlink="{self.base}/collections/stout-shelf"', html)
        # a name without a collection has no link
        self.assertIn('data-rname="From one" data-rtext="" data-rlink=""', html)
        # an answer with no result at all
        self.assertIn('data-rname="" data-rtext="" data-rlink=""', html)

    def test_match_quiz_priority_order_and_popup_behaviour_are_in_the_section_file(self):
        code = self.ok("get", "/api/v1/themes/files/")["files"]
        script = next(f["content"] for f in code if f["path"] == "sections/match-quiz.liquid")
        # questions are checked in the order of "Question priority"; the first chosen answer with a name or link wins
        self.assertIn("function priorityOrder()", script)
        self.assertIn("var pick = picks[order[i]];", script)
        self.assertIn("if (pick && (pick.name || pick.link))", script)
        self.assertIn('setResult(pick.name, pick.text, pick.link, "Shop Now")', script)
        self.assertIn("text-transform: uppercase", script)  # the button reads SHOP NOW
        # advances 350ms after an answer; no answers kept after reopening; page scroll is locked while open
        self.assertIn("}, 350);", script)
        self.assertIn("picks = [];", script)
        self.assertIn('document.body.style.overflow = "hidden"', script)
        self.assertIn("back.hidden = index === 0;", script)
        self.assertIn("data-mq-close", script)
        self.assertIn('event.key === "Escape"', script)
        self.assertIn("@media (max-width: 749px)", script)
        self.assertIn("grid-template-columns: 1fr 0.9fr", script)

    def test_match_quiz_most_picks_still_works_when_chosen_or_when_answers_have_no_results(self):
        self.add_sections("index", {"match": {"type": "match-quiz", "settings": {"result_logic": "picks"},
                                              "blocks": self.PRIORITY_BLOCKS + self.MATCH_BLOCKS[2:]}})
        html = self.client.get(f"{self.base}/").content.decode()
        self.assertIn('data-mode="picks"', html)
        self.assertIn('data-key="cider"', html)
        # "Priority order" with answers that have no results (an older section) falls back to the Result blocks
        self.add_sections("index", {"older": {"type": "match-quiz", "settings": {}, "blocks": self.MATCH_BLOCKS}})
        html = self.client.get(f"{self.base}/").content.decode()
        self.assertEqual(html.count('data-mode="picks"'), 2)

    # ------------------------------------------------------------------ Shop Now goes to the chosen collection
    def renamed_unpublished_collection(self):
        """"Beer" renamed to "Beer & Cider": its address stays /collections/beer, and it is off the Online Store."""
        made = self.ok("post", "/api/v1/catalog/collections/", {"name": "Beer", "sales_channels": []}, expect=201)
        self.assertEqual(made["handle"], "beer")
        return self.ok("patch", f"/api/v1/catalog/collections/{made['id']}/", {"name": "Beer & Cider"})

    def quiz_links(self, *values):
        blocks = [{"type": "question", "settings": {
            "question": "Q", **{f"answer_{n}_label": f"A{n}" for n in range(1, len(values) + 1)},
            **{f"answer_{n}_link": value for n, value in enumerate(values, 1)}}}]
        self.add_sections("index", {"match": {"type": "match-quiz", "settings": {}, "blocks": blocks}})
        html = self.client.get(f"{self.base}/").content.decode()
        return re.findall(r'data-rname="([^"]*)" data-rtext="[^"]*" data-rlink="([^"]*)"', html)

    def test_match_quiz_answer_goes_to_the_chosen_collection_by_id_handle_name_or_old_slug(self):
        beer = self.renamed_unpublished_collection()
        target = f"{self.base}/collections/beer"

        results = self.quiz_links(
            beer["id"],                    # what the customizer saves
            "beer",                        # its handle
            "Beer & Cider",                # its current name
            "beer-cider",                  # a link made from the new title before the handle was known
        )

        self.assertEqual(results, [("Beer &amp; Cider", target)] * 4)

    def test_match_quiz_answer_accepts_a_collection_address_and_keeps_other_links(self):
        self.renamed_unpublished_collection()
        self.ok("post", "/api/v1/catalog/collections/", {"name": "Stout shelf"}, expect=201)

        results = self.quiz_links("/collections/beer-cider", "Stout shelf", "/pages/about", "https://example.com/x")

        self.assertEqual([url for _, url in results], [
            f"{self.base}/collections/beer",            # the address of a renamed collection leads to it
            f"{self.base}/collections/stout-shelf",
            f"{self.base}/pages/about",                 # not a collection: kept as it is
            "https://example.com/x",
        ])

    def test_match_quiz_a_real_handle_wins_over_another_collections_name_slug(self):
        self.renamed_unpublished_collection()  # its name slugs to beer-cider
        other = self.ok("post", "/api/v1/catalog/collections/", {"name": "Other", "handle": "beer-cider"}, expect=201)

        results = self.quiz_links("beer-cider", other["id"])

        self.assertEqual([url for _, url in results], [f"{self.base}/collections/beer-cider"] * 2)

    def test_match_quiz_result_blocks_go_to_the_chosen_collection_too(self):
        beer = self.renamed_unpublished_collection()
        blocks = [
            {"type": "question", "settings": {"question": "Q", "answer_1_label": "A", "answer_1_key": "beer"}},
            {"type": "result", "settings": {"result_key": "beer", "name": "Beer", "link": f"/collections/{beer['id']}"}},
            {"type": "result", "settings": {"result_key": "named", "name": "Named", "link": "/collections/beer-cider"}},
            {"type": "result", "settings": {"result_key": "all", "name": "All"}},  # default: /collections/all
            {"type": "result", "settings": {"result_key": "web", "name": "Web", "link": "https://example.com/x"}},
        ]
        self.add_sections("index", {"match": {"type": "match-quiz", "settings": {"result_logic": "picks"}, "blocks": blocks}})

        html = self.client.get(f"{self.base}/").content.decode()

        links = re.findall(r'data-mq-result data-key="([^"]*)" data-link="([^"]*)"', html)
        self.assertEqual(links, [
            ("beer", f"{self.base}/collections/beer"),
            ("named", f"{self.base}/collections/beer"),
            ("all", f"{self.base}/collections/all"),
            ("web", "https://example.com/x"),
        ])

    def test_customizer_preview_opens_an_unpublished_chosen_collection_not_all_products(self):
        beer = self.renamed_unpublished_collection()
        settings = self.ok("get", "/api/v1/themes/settings/")

        html = self.ok("post", f"/api/v1/themes/preview/?page=collection&collection={beer['id']}", settings)["html"]

        self.assertIn("Beer &amp; Cider", html)
        self.assertNotIn("All products", html)
        # the live storefront still keeps it hidden until it is on the Online Store
        self.assertEqual(self.client.get(f"{self.base}/collections/beer").status_code, 404)

    def test_match_quiz_limits_and_placeholders(self):
        self.add_sections("index", {"match": {"type": "match-quiz", "settings": {}}})
        page = self.client.get(f"{self.base}/").content.decode()
        self.assertNotIn("<button", page.split('class="section match-quiz', 1)[1].split("</section>", 1)[0])
        self.assertNotIn("data-mq-open", page)  # no button, popup or script without blocks
        preview = self.ok("post", "/api/v1/themes/preview/", self.ok("get", "/api/v1/themes/settings/"))["html"]
        self.assertIn('Add "Question" blocks', preview)
        data = self.ok("get", "/api/v1/themes/settings/")
        section = data["templates"]["index"]["sections"]["match"]
        section["blocks"] = [{"type": "question", "settings": {}} for _ in range(9)]
        r = self.client.patch("/api/v1/themes/settings/", {"templates": {"index": {
            "order": data["templates"]["index"]["order"], "sections": {"match": section}}}}, format="json")
        self.assertEqual(r.status_code, 400, r.content[:300])

    def test_match_quiz_edit_code_change_shows_for_that_store_only(self):
        other_token, other = self.sign_up("other-quiz@example.com", "Other Quiz Store")
        for token, store in ((self.token, self.store), (other_token, other)):
            self.use(token, store["storeId"])
            self.add_sections("index", {"match": {"type": "match-quiz", "settings": {}, "blocks": self.MATCH_BLOCKS}})
        self.use(self.token, self.store["storeId"])
        original = next(f["content"] for f in self.ok("get", "/api/v1/themes/files/")["files"]
                        if f["path"] == "sections/match-quiz.liquid")
        edited = original.replace("Your recommendation", "Hand-edited recommendation")
        self.assertNotEqual(edited, original)
        self.ok("post", "/api/v1/themes/files/", {"path": "sections/match-quiz.liquid", "content": edited})

        mine = self.client.get(f"{self.base}/").content.decode()
        theirs = self.client.get(f"/s/{other['storeSlug']}/").content.decode()
        self.assertIn("Hand-edited recommendation", mine)
        self.assertNotIn("Hand-edited recommendation", theirs)
        self.assertIn("Your recommendation", theirs)
        preview = self.ok("post", "/api/v1/themes/preview/", self.ok("get", "/api/v1/themes/settings/"))["html"]
        self.assertIn("Hand-edited recommendation", preview)
        from .services import starter_files
        self.assertIn("Your recommendation", starter_files()["sections/match-quiz.liquid"])

    def test_match_quiz_question_added_in_index_json_reaches_the_customizer_and_the_store(self):
        from . import section_sync as sync
        from .models import StoreTheme

        self.ok("post", "/api/v1/catalog/collections/", {"name": "Stout shelf", "status": "active"}, expect=201)
        self.add_sections("index", {"match": {"type": "match-quiz", "settings": {}, "blocks": self.PRIORITY_BLOCKS[:1]}})
        store_id = StoreTheme.objects.get(store__store_id=self.store["storeId"], is_active=True).store_id

        def index_json():
            files = self.ok("get", "/api/v1/themes/files/")["files"]
            return json.loads(next(f["content"] for f in files if f["path"] == "templates/index.json"))

        code = index_json()
        new_question = {"type": "question", "settings": {
            "question": "Brand new question", "answer_1_label": "Dark", "answer_1_result_name": "Stouts",
            "answer_1_result_text": "Roasty and rich.", "answer_1_link": "stout-shelf"}}
        code["sections"]["match"]["blocks"].append(new_question)
        seen = sync.EVENTS.last_seq
        self.ok("post", "/api/v1/themes/files/", {"path": "templates/index.json", "content": json.dumps(code, indent=2)})

        # 1. the customizer's sidebar data has the new Question block ...
        blocks = self.ok("get", "/api/v1/themes/settings/")["templates"]["index"]["sections"]["match"]["blocks"]
        self.assertEqual(blocks[-1]["type"], "question")
        self.assertEqual(blocks[-1]["settings"]["question"], "Brand new question")
        self.assertEqual(blocks[-1]["settings"]["answer_1_link"], "stout-shelf")
        # ... and an open customizer is told right away, with the section's blocks
        events = [p for _, p in sync.EVENTS.wait(store_id, seen, 0)[0] if p.get("kind") == "template"]
        self.assertTrue(events, "no live event for the open customizer")
        self.assertEqual(events[-1]["source"], "code")
        self.assertEqual(events[-1]["template"]["sections"]["match"]["blocks"][-1]["settings"]["question"], "Brand new question")
        # 2. the storefront and the customizer preview show it
        settings = self.ok("get", "/api/v1/themes/settings/")
        for html in (self.client.get(f"{self.base}/").content.decode(), self.ok("post", "/api/v1/themes/preview/", settings)["html"]):
            self.assertIn("Brand new question", html)
            self.assertIn(f'data-rname="Stouts" data-rtext="Roasty and rich." data-rlink="{self.base}/collections/stout-shelf"', html)
        # 3. the other way round: a block added in the customizer shows in index.json
        template = settings["templates"]["index"]
        template["sections"]["match"]["blocks"].append({"type": "question", "settings": {"question": "From the customizer"}})
        self.ok("patch", "/api/v1/themes/settings/", {"templates": {"index": template}})
        questions = [b["settings"]["question"] for b in index_json()["sections"]["match"]["blocks"]]
        self.assertEqual(questions[-2:], ["Brand new question", "From the customizer"])

        # 4. invalid JSON and invalid block settings are refused with a line number, and nothing is saved
        good = json.dumps(index_json(), indent=2)
        r = self.client.post("/api/v1/themes/files/", {"path": "templates/index.json", "content": good[:-3]}, format="json")
        self.assertEqual(r.status_code, 400, r.content[:300])
        self.assertIn("not valid JSON", r.content.decode())
        self.assertRegex(r.content.decode(), r"line \d+")

        bad = index_json()
        bad["sections"]["match"]["blocks"][1]["settings"]["no_such_setting"] = "x"
        text = json.dumps(bad, indent=2)
        r = self.client.post("/api/v1/themes/files/", {"path": "templates/index.json", "content": text}, format="json")
        self.assertEqual(r.status_code, 400, r.content[:300])
        body = r.content.decode()
        self.assertIn("blocks[1]", body)
        expected_line = next(i for i, line in enumerate(text.splitlines(), 1) if line.strip().startswith('"no_such_setting"'))
        self.assertIn(f"(line {expected_line})", body)
        self.assertEqual(index_json(), json.loads(good))  # nothing was saved

    def test_match_quiz_and_unknown_sections_resolve_without_error(self):
        from .schemas import resolve
        custom_data = {
            "templates": {
                "index": {
                    "order": ["mq", "unknown"],
                    "sections": {
                        "mq": {
                            "type": "match-quiz",
                            "settings": {"heading": "Find your match"},
                            "blocks": [
                                {"type": "question", "settings": {"question": "Your preference?", "answer_1_label": "Tea", "answer_1_key": "tea"}},
                                {"type": "result", "settings": {"name": "Green Tea", "result_key": "tea"}},
                            ],
                        },
                        "unknown": {
                            "type": "custom-nonexistent-type",
                            "settings": {"heading": "Custom"},
                        },
                    },
                }
            }
        }
        resolved = resolve(custom_data)
        self.assertIn("mq", resolved["templates"]["index"]["sections"])
        # Sections of a type that no longer exists are dropped, from both sections and order
        self.assertNotIn("unknown", resolved["templates"]["index"]["sections"])
        self.assertEqual(resolved["templates"]["index"]["order"], ["mq"])
        self.assertEqual(resolved["templates"]["index"]["sections"]["mq"]["settings"]["heading"], "Find your match")

    def test_newsletter_subscribes_an_email_once_and_rejects_a_bad_one(self):
        from apps.customers.models import Customer

        url = f"{self.base}/newsletter"
        bad = self.client.post(url, {"email": "nope"}, format="json")
        self.assertEqual(bad.status_code, 400)
        for _ in range(2):
            ok = self.client.post(url, {"email": "Fan@Example.com"}, format="json")
            self.assertEqual(ok.status_code, 200)
        customers = Customer.objects.filter(store__store_slug=self.store["storeSlug"], email="fan@example.com")
        self.assertEqual(customers.count(), 1)
        self.assertEqual(customers.first().email_subscription_status, "subscribed")

    def test_newsletter_resubscribes_a_customer_who_had_unsubscribed(self):
        from apps.customers.models import Customer
        from apps.tenancy.models import Store

        store = Store.objects.get(store_slug=self.store["storeSlug"])
        Customer.objects.create(store=store, email="back@example.com", email_subscription_status="unsubscribed")
        self.assertEqual(self.client.post(f"{self.base}/newsletter", {"email": "back@example.com"}, format="json").status_code, 200)
        self.assertEqual(Customer.objects.get(store=store, email="back@example.com").email_subscription_status, "subscribed")

    def test_utility_bar_is_part_of_the_footer_section(self):
        self.assertNotIn('class="page-width site-footer__utility-inner"', self.client.get(f"{self.base}/").content.decode())
        footer = self.ok("get", "/api/v1/themes/sections/")["footer"]["values"]
        values = {"settings": {**footer["settings"], "utility_show": True, "utility_left": "Free delivery over 999",
                               "utility_phone": "+91 99999 00000", "utility_menu": "Track order | /account"},
                  "blocks": footer["blocks"]}
        self.ok("patch", "/api/v1/themes/settings/", {"sections": {"footer": values}})
        html = self.client.get(f"{self.base}/").content.decode()
        self.assertIn("Free delivery over 999", html)
        self.assertIn('href="tel:+9199999' + '00000"', html)
        self.assertIn(f'href="{self.base}/account">Track order</a>', html)

    def test_announcement_bar_is_part_of_the_header_section(self):
        header = self.ok("get", "/api/v1/themes/sections/")["header"]["values"]
        values = {"settings": {**header["settings"], "announcement_text": "Big sale today"}, "blocks": header["blocks"]}
        self.ok("patch", "/api/v1/themes/settings/", {"sections": {"header": values}})
        html = self.client.get(f"{self.base}/").content.decode()
        self.assertIn("Big sale today", html)
        self.assertLess(html.index("Big sale today"), html.index('class="site-header"'))

    def test_announcement_bar_rotates_its_messages(self):
        header = self.ok("get", "/api/v1/themes/sections/")["header"]["values"]
        blocks = header["blocks"] + [
            {"type": "announcement", "settings": {"text": "New arrivals", "link": "/collections/all"}},
            {"type": "announcement", "settings": {"text": "Gift wrapping", "link": ""}},
        ]
        values = {"settings": {**header["settings"], "announcement_text": "Big sale today"}, "blocks": blocks}
        self.ok("patch", "/api/v1/themes/settings/", {"sections": {"header": values}})
        html = self.client.get(f"{self.base}/").content.decode()
        self.assertIn('aria-label="Previous announcement"', html)
        self.assertIn('aria-label="2 of 3"', html)
        self.assertIn(">New arrivals</a>", html)
        self.assertIn("solid #D4AF37", html)
        self.assertLess(html.index("Gift wrapping"), html.index('class="site-header"'))

    def test_search_lists_only_matching_products(self):
        html = self.client.get(f"{self.base}/collections/all?q=mug").content.decode()
        self.assertIn("Search results for &ldquo;mug&rdquo;", html)
        self.assertIn("Blue mug", html)
        self.assertNotIn("Red scarf", html)
        none = self.client.get(f"{self.base}/collections/all?q=zzzz").content.decode()
        self.assertIn("No products match these filters", none)

    def test_common_search_functionality_across_storefront_and_preview(self):
        # 1. Direct /search route
        res = self.client.get(f"{self.base}/search?q=mug")
        self.assertEqual(res.status_code, 200)
        html = res.content.decode()
        self.assertIn("Blue mug", html)
        self.assertNotIn("Red scarf", html)

        # 2. Case-insensitivity and whitespace trimming
        res = self.client.get(f"{self.base}/search?q=  MuG  ")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Blue mug", res.content.decode())

        # 3. Partial keyword matching
        res = self.client.get(f"{self.base}/search?q=carf")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Red scarf", res.content.decode())
        self.assertNotIn("Blue mug", res.content.decode())

        # 4. JSON endpoint format
        res_json = self.client.get(f"{self.base}/search?q=mug&format=json")
        self.assertEqual(res_json.status_code, 200)
        data = res_json.json()
        self.assertEqual(data.get("q"), "mug")
        self.assertEqual(len(data.get("products", [])), 1)
        self.assertEqual(data["products"][0]["title"], "Blue mug")

        # 5. Empty search results
        none = self.client.get(f"{self.base}/search?q=nonexistent123")
        self.assertEqual(none.status_code, 200)
        self.assertIn("No products match these filters", none.content.decode())
        self.assertIn("Clear search", none.content.decode())

        # 6. Theme customizer preview query support
        settings = self.ok("get", "/api/v1/themes/settings/")
        prev_html = self.ok("post", "/api/v1/themes/preview/?page=collection&q=mug", settings)["html"]
        self.assertIn("Blue mug", prev_html)
        self.assertNotIn("Red scarf", prev_html)

    def test_product_cards_carry_badges(self):
        html = self.client.get(f"{self.base}/collections/all").content.decode()
        self.assertIn('product-card__badge product-card__badge--new">New</span>', html)

    def test_product_badges_order_and_limit(self):
        from .rendering import product_badges

        self.assertEqual(product_badges(["badge:Best seller", "badge:Organic"], False, True, True), ["Few left", "Best seller"])
        self.assertEqual(product_badges(["x"], True, False, True), ["Sold out", "New"])
        self.assertEqual(product_badges(["badge:"], False, False, False), [])

    def test_product_page_has_zoom_and_tier_cards(self):
        from apps.catalog.models import ProductVariant

        page = f"{self.base}/products/{self.mug['id']}"
        html = self.client.get(page).content.decode()
        self.assertIn('data-zoom="hover"', html)
        self.assertIn("product__main-image--zoom-hover", html)
        self.assertNotIn("data-tiers", html)  # one option: nothing to choose

        ProductVariant.objects.create(product_id=self.mug["id"], sku="Gold tier", price="25.00")
        html = self.client.get(page).content.decode()
        self.assertIn("data-tiers", html)
        # Customers never see the SKU or a price inside the buttons
        self.assertIn(">Option 2</button>", html)
        self.assertNotIn("Gold tier</button>", html)
        self.assertNotIn("product__tier-price", html)
        self.assertNotIn('<span class="product__tier-name">', html)
        self.assertIn("25.00", html)
        self.assertIn("<select name=\"variant\" data-variant>", html)  # the form still posts the select

        data = self.ok("get", "/api/v1/themes/settings/")
        main = next(s for s in data["templates"]["product"]["sections"].values() if s["type"] == "main-product")
        main["settings"].update(variant_style="dropdown", image_zoom="lightbox")
        self.ok("patch", "/api/v1/themes/settings/", {"templates": {"product": {
            "order": data["templates"]["product"]["order"], "sections": data["templates"]["product"]["sections"]}}})
        html = self.client.get(page).content.decode()
        self.assertNotIn("data-tiers", html)
        self.assertIn('data-zoom="lightbox"', html)


    def test_product_page_picker_uses_option_name_and_values_only(self):
        from apps.catalog.models import Product, ProductVariant

        product = Product.objects.get(pk=self.mug["id"])
        product.options = [{"name": "Pack size", "values": ["Single", "Pack of 4"]}]
        product.save(update_fields=["options"])
        first = ProductVariant.objects.filter(product=product).first()
        first.sku = "ABC123"
        first.metadata = {"options": {"Pack size": "Single"}}
        first.save()
        ProductVariant.objects.create(product=product, sku="ABC123-PACKOF4", price="55.00", metadata={"options": {"Pack size": "Pack of 4"}})

        html = self.client.get(f"{self.base}/products/{self.mug['id']}").content.decode()
        self.assertIn('<span class="product__option-name">Pack size</span>', html)
        self.assertIn(">Single</button>", html)
        self.assertIn(">Pack of 4</button>", html)
        self.assertEqual(html.count("data-option-group "), 1)  # one picker
        self.assertEqual(html.count('<select name="variant" data-variant hidden>'), 1)  # the dropdown is only the hidden form field
        self.assertNotIn("product__tier-price", html)
        picker = html.split("data-options>")[1].split("</form>")[0]
        self.assertNotIn("ABC123-PACKOF4", picker.split("<select")[0])
        self.assertNotIn("Option</span>", html)
        # data-opt-0 is read with getAttribute: dataset["opt0"] would never match it and every click would pick the first variant
        self.assertIn('getAttribute("data-opt-" + keys[k])', html)


    def test_product_page_untracked_variants_are_buyable_and_first_available_is_shown(self):
        from apps.catalog.models import Product, ProductVariant
        from apps.inventory.services import default_location, set_available
        from apps.tenancy.models import Store

        product = Product.objects.get(pk=self.mug["id"])
        product.options = [{"name": "Pack size", "values": ["Single", "Pack of 4"]}]
        product.save(update_fields=["options"])
        first = ProductVariant.objects.filter(product=product).order_by("created_at").first()
        first.metadata = {"options": {"Pack size": "Single"}}
        first.save()
        store = Store.objects.get(store_slug=self.store["storeSlug"])
        set_available(first, default_location(store), 0)  # tracked, sold out
        untracked = ProductVariant.objects.create(product=product, sku="P4", price="55.00", metadata={"options": {"Pack size": "Pack of 4"}})

        html = self.client.get(f"{self.base}/products/{self.mug['id']}").content.decode()
        self.assertIn('data-option-value="Single"', html)
        # the tracked, sold-out one is muted; the untracked one is not
        single = html.split('data-option-value="Single"')[0].rsplit("<button", 1)[1]
        pack = html.split('data-option-value="Pack of 4"')[0].rsplit("<button", 1)[1]
        self.assertIn("is-muted", single)
        self.assertNotIn("is-muted", pack)
        # the page opens on the untracked pack, not the sold-out single
        self.assertRegex(html, r'data-option-value="Pack of 4"\s+aria-checked="true"')
        self.assertRegex(html, r'data-option-value="Single"\s+aria-checked="false"')
        self.assertIn(f'<option value="{untracked.id}" selected', html)
        self.assertIn('data-variant-continue="true"', html)  # the script never treats it as sold out
        self.assertNotIn(" disabled>Sold out", html)
        # the price at load is the selected variant's, not the product's cheapest ("From")
        price = html.split("data-price>")[1].split("</div>")[0]
        self.assertIn("55.00", price)
        self.assertNotIn("From", price)

    def test_varying_variant_prices_show_the_cheapest_price_without_from(self):
        import re

        from apps.catalog.models import Product, ProductVariant

        product = Product.objects.get(pk=self.mug["id"])
        ProductVariant.objects.create(product=product, sku="MUG-BIG", price="40.00")
        ProductVariant.objects.filter(product=product, sku="SKU-MUG").update(price="15.00")

        for path in ("/collections/all", "/", f"/products/{product.id}"):
            html = self.client.get(f"{self.base}{path}").content.decode()
            prices = re.findall(r'<span class="price">([^<]*)</span>', html)
            self.assertTrue(any("15.00" in p for p in prices), (path, prices))
            self.assertFalse(any("From" in p for p in prices), (path, prices))
            self.assertNotRegex(html, r">\s*From\s*[^<\s]*\d")

    def test_sync_price_from_changes_only_the_named_theme_and_keeps_a_backup(self):
        import json
        import tempfile
        from io import StringIO
        from pathlib import Path

        from django.core.management import call_command

        from .models import StoreTheme, Theme

        old = "From {{ product.price | money }}"
        self.ok("get", "/api/v1/themes/settings/")  # the store gets its theme on first use
        mine =StoreTheme.objects.get(store__store_id=self.store["storeId"], is_active=True).theme
        other = Theme.objects.create(name="Another store's theme")
        for theme in (mine, other):
            theme.files.update_or_create(file_path="snippets/product-card.liquid", defaults={
                "file_name": "product-card.liquid", "directory": "snippets", "file_type": "liquid", "content": f"<span>{old}</span>"})

        with tempfile.TemporaryDirectory() as backups:
            call_command("sync_price_from", theme=str(mine.pk), backup_dir=backups, stdout=StringIO())  # dry run
            self.assertIn(old, mine.files.get(file_path="snippets/product-card.liquid").content)
            call_command("sync_price_from", theme=str(mine.pk), apply=True, backup_dir=backups, stdout=StringIO())
            saved = json.loads(next(Path(backups).glob("*.json")).read_text(encoding="utf-8"))

        self.assertEqual(mine.files.get(file_path="snippets/product-card.liquid").content, "<span>{{ product.price | money }}</span>")
        self.assertEqual(saved["files"]["snippets/product-card.liquid"], f"<span>{old}</span>")  # the old file is kept
        self.assertIn(old, other.files.get(file_path="snippets/product-card.liquid").content)  # other themes untouched


class ThemeImageUrlTest(StoreClientMixin, APITestCase):
    from apps.media.tests import PNG

    def setUp(self):
        self.token, self.store = self.sign_up("imgurl@example.com", "Img Store")

    def upload(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        r = self.client.post("/api/v1/themes/images/", {"image": SimpleUploadedFile("a.png", self.PNG)}, format="multipart")
        self.assertEqual(r.status_code, 201)
        return r.json()["url"]

    def test_logo_upload_saves_and_renders_on_storefront(self):
        url = self.upload()
        self.assertRegex(url, r"/s/_img/[0-9a-f-]{36}$")
        header = self.ok("get", "/api/v1/themes/sections/")["header"]["values"]
        header["settings"]["logo_image"] = url
        self.ok("patch", "/api/v1/themes/settings/", {"sections": {"header": header}})
        for _ in range(2):  # again after a "refresh"
            html = self.client.get(f"/s/{self.store['storeSlug']}/").content.decode()
            self.assertIn(f'<img src="{url}"', html)
        self.assertEqual(self.client.get(url.split("testserver")[-1]).status_code, 200)

    def test_url_is_https_in_production(self):
        from django.test import override_settings
        with override_settings(DEBUG=False):
            self.assertTrue(self.upload().startswith("https://"))


class PublicImageUrlAndCorsTest(StoreClientMixin, APITestCase):
    from apps.media.tests import PNG

    def setUp(self):
        self.token, self.store = self.sign_up("imgcors@example.com", "Cors Store")

    def upload(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        r = self.client.post("/api/v1/themes/images/", {"image": SimpleUploadedFile("a.png", self.PNG)}, format="multipart")
        self.assertEqual(r.status_code, 201)
        return r.json()["url"]

    def test_upload_url_uses_public_api_url_when_set(self):
        from django.test import override_settings
        with override_settings(PUBLIC_API_URL="https://api.example.com"):
            self.assertRegex(self.upload(), r"^https://api\.example\.com/s/_img/[0-9a-f-]{36}$")

    def test_upload_url_falls_back_to_request_host(self):
        from django.test import override_settings
        with override_settings(PUBLIC_API_URL=""):
            self.assertRegex(self.upload(), r"^https?://testserver/s/_img/[0-9a-f-]{36}$")

    def test_image_is_readable_from_a_sandboxed_page_without_credentials(self):
        path = self.upload().split("testserver")[-1]
        r = self.client.get(path, HTTP_ORIGIN="null")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Access-Control-Allow-Origin"], "*")
        self.assertEqual(r["Cross-Origin-Resource-Policy"], "cross-origin")
        self.assertNotIn("Access-Control-Allow-Credentials", r)

    def test_bucket_redirect_carries_the_same_headers(self):
        from apps.media.models import MediaAsset
        asset = MediaAsset.objects.filter(store_id=self.store["storeId"]).first() or MediaAsset.objects.create(
            store_id=self.store["storeId"], mime_type="image/png", file_size=1, data=self.PNG, original_name="a.png")
        MediaAsset.objects.filter(pk=asset.pk).update(storage_key="k/a.png", data=None)
        from unittest import mock
        with mock.patch.object(MediaAsset, "url", new_callable=mock.PropertyMock, return_value="https://cdn.example.com/k/a.png"):
            r = self.client.get(f"/s/_img/{asset.pk}", HTTP_ORIGIN="null")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Access-Control-Allow-Origin"], "*")
        self.assertNotIn("Access-Control-Allow-Credentials", r)

    def test_api_cors_stays_restricted(self):
        r = self.client.get("/api/v1/themes/sections/", HTTP_ORIGIN="null")
        self.assertNotEqual(r.get("Access-Control-Allow-Origin"), "*")
        self.assertNotEqual(r.get("Access-Control-Allow-Origin"), "null")


class MenusApiTest(StoreClientMixin, APITestCase):
    URL = "/api/v1/themes/menus/"

    def setUp(self):
        self.sign_up("menus@example.com", "Menu Store")

    def test_crud_with_nested_items(self):
        menu = self.ok("post", self.URL, {
            "name": "Main menu",
            "items": [
                {"title": "Shop", "url": "/collections/all", "children": [{"title": "New", "url": "/collections/new"}]},
                {"title": "About", "url": "/pages/about"},
                {"title": "", "url": "/blank-rows-are-dropped"},
            ],
        }, expect=201)
        self.assertEqual(menu["handle"], "main-menu")
        self.assertEqual(menu["items_summary"], "Shop, About")
        self.assertEqual(menu["items_count"], 3)
        self.assertEqual(menu["items"][0]["children"][0]["title"], "New")

        # PUT replaces the items; a PATCH without items leaves them alone
        url = f"{self.URL}{menu['id']}/"
        self.ok("patch", url, {"name": "Header"})
        self.assertEqual(self.ok("get", url)["items_count"], 3)
        updated = self.ok("put", url, {"name": "Header", "items": [{"title": "Home", "url": "/"}]})
        self.assertEqual(updated["items_summary"], "Home")
        self.assertEqual(len(self.ok("get", self.URL)), 1)

        self.ok("delete", url, expect=204)
        self.assertEqual(self.ok("get", self.URL), [])

    def test_item_order_is_kept(self):
        menu = self.ok("post", self.URL, {"name": "Order", "items": [
            {"title": "B", "url": "/b", "order": 1}, {"title": "A", "url": "/a", "order": 0},
        ]}, expect=201)
        self.assertEqual([i["title"] for i in menu["items"]], ["A", "B"])

    def test_unsafe_links_are_rejected(self):
        for bad in ("javascript:alert(1)", "data:text/html,x", "//evil.example"):
            r = self.client.post(self.URL, {"name": "Evil", "items": [{"title": "x", "url": bad}]}, format="json")
            self.assertEqual(r.status_code, 400, bad)
        r = self.client.post(self.URL, {"name": "Evil", "items": [
            {"title": "ok", "url": "/", "children": [{"title": "x", "url": "javascript:1"}]}]}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.ok("get", self.URL), [])  # nothing half-saved

    def test_malformed_items_are_a_400_not_a_500(self):
        for items in ("nope", [1], [{"title": "x", "children": "no"}]):
            r = self.client.post(self.URL, {"name": "Bad", "items": items}, format="json")
            self.assertEqual(r.status_code, 400, items)
        # null url and mailto/tel/external links are fine
        self.ok("post", self.URL, {"name": "Fine", "items": [
            {"title": "a", "url": None}, {"title": "b", "url": "mailto:a@b.co"},
            {"title": "c", "url": "https://example.com"}]}, expect=201)

    def test_names_and_handles_are_unique_per_store(self):
        self.ok("post", self.URL, {"name": "Footer", "handle": "footer"}, expect=201)
        duplicate_name = self.client.post(self.URL, {"name": "  footer  "}, format="json")
        self.assertEqual(duplicate_name.status_code, 400)
        self.assertIn("name", duplicate_name.data)
        duplicate_handle = self.client.post(
            self.URL, {"name": "Other", "handle": "footer"}, format="json"
        )
        self.assertEqual(duplicate_handle.status_code, 400)
        self.assertIn("handle", duplicate_handle.data)
        header = self.ok("post", self.URL, {"name": "Header"}, expect=201)
        duplicate_update = self.client.patch(
            f"{self.URL}{header['id']}/", {"handle": "footer"}, format="json"
        )
        self.assertEqual(duplicate_update.status_code, 400)
        self.assertIn("handle", duplicate_update.data)

    def test_same_name_and_handle_are_allowed_in_different_stores(self):
        self.ok("post", self.URL, {"name": "Main", "handle": "main"}, expect=201)
        self.sign_up("other-menu-store@example.com", "Other Menu Store")
        self.ok("post", self.URL, {"name": "Main", "handle": "main"}, expect=201)

    def test_generated_handles_avoid_collisions_on_create_and_rename(self):
        first = self.ok("post", self.URL, {"name": "Main menu"}, expect=201)
        second = self.ok("post", self.URL, {"name": "Header", "handle": "header"}, expect=201)
        self.assertEqual(first["handle"], "main-menu")

        renamed = self.ok("patch", f"{self.URL}{second['id']}/", {"name": "Main-menu"})
        self.assertEqual(renamed["handle"], "main-menu-2")

        third = self.ok("post", self.URL, {"name": "Main--menu"}, expect=201)
        self.assertEqual(third["handle"], "main-menu-3")

    def test_menu_constraints_reject_duplicate_direct_writes(self):
        from django.db import IntegrityError, transaction

        from apps.themes.models import Menu
        from apps.tenancy.models import Store

        store = Store.objects.get(store_name="Menu Store")
        Menu.objects.create(store=store, name="Normalized Name", handle="normalized-name")

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Menu.objects.create(store=store, name=" normalized name ", handle="different")

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Menu.objects.create(store=store, name="Different Name", handle="normalized-name")

    def test_menu_items_reject_invalid_parent_relationships(self):
        from django.core.exceptions import ValidationError as DjangoValidationError

        from apps.themes.models import Menu, MenuItem
        from apps.tenancy.models import Store

        store = Store.objects.get(store_name="Menu Store")
        menu = Menu.objects.create(store=store, name="Parent menu", handle="parent-menu")
        other_menu = Menu.objects.create(store=store, name="Other parent menu", handle="other-menu")
        parent = MenuItem.objects.create(menu=menu, title="Parent")
        child = MenuItem.objects.create(menu=menu, parent=parent, title="Child")

        with self.assertRaises(DjangoValidationError):
            MenuItem.objects.create(menu=other_menu, parent=parent, title="Cross-menu child")

        parent.parent = parent
        with self.assertRaises(DjangoValidationError):
            parent.save()

        parent.parent = child
        with self.assertRaises(DjangoValidationError):
            parent.save()

    def test_invalid_item_replacement_preserves_existing_items(self):
        menu = self.ok("post", self.URL, {
            "name": "Preserve items",
            "items": [{"title": "Existing", "url": "/existing"}],
        }, expect=201)
        url = f"{self.URL}{menu['id']}/"

        response = self.client.put(url, {
            "name": "Preserve items",
            "items": [{"title": "Invalid", "url": "javascript:alert(1)"}],
        }, format="json")

        self.assertEqual(response.status_code, 400)
        current = self.ok("get", url)
        self.assertEqual(current["items_count"], 1)
        self.assertEqual(current["items"][0]["title"], "Existing")

    def test_menus_are_private_to_their_store(self):
        menu = self.ok("post", self.URL, {"name": "Mine"}, expect=201)
        self.sign_up("other@example.com", "Other Store")
        self.assertEqual(self.ok("get", self.URL), [])
        self.assertEqual(self.client.get(f"{self.URL}{menu['id']}/").status_code, 404)
        self.assertEqual(self.client.delete(f"{self.URL}{menu['id']}/").status_code, 404)

    def test_list_query_count_does_not_grow_with_items(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def list_queries():
            with CaptureQueriesContext(connection) as ctx:
                self.ok("get", self.URL)
            return len(ctx)

        self.ok("post", self.URL, {"name": "M0", "items": [{"title": "t", "url": "/"}]}, expect=201)
        small = list_queries()
        for n in range(1, 4):
            self.ok("post", self.URL, {"name": f"M{n}", "items": [
                {"title": f"t{i}", "url": "/", "children": [{"title": "c", "url": "/"}]} for i in range(5)]}, expect=201)
        self.assertEqual(list_queries(), small)

    def test_mega_menu_fields_and_three_level_nesting_crud(self):
        payload = {
            "name": "Mega Navigation",
            "handle": "mega-nav",
            "items": [
                {
                    "title": "Products",
                    "url": "/collections/all",
                    "is_mega": True,
                    "badge": "Hot",
                    "promo_image": "https://example.com/banner.jpg",
                    "promo_title": "Mega Sale",
                    "promo_subtitle": "Up to 50% Off",
                    "promo_url": "/collections/sale",
                    "children": [
                        {
                            "title": "Wine",
                            "url": "/collections/wine",
                            "badge": "Popular",
                            "children": [
                                {"title": "Red Wine", "url": "/collections/red-wine"},
                                {"title": "White Wine", "url": "/collections/white-wine"},
                            ],
                        },
                        {
                            "title": "Spirits",
                            "url": "/collections/spirits",
                            "children": [
                                {"title": "Whisky", "url": "/collections/whisky"},
                                {"title": "Gin", "url": "/collections/gin"},
                            ],
                        },
                    ],
                },
                {"title": "About", "url": "/pages/about"},
            ],
        }

        created = self.ok("post", self.URL, payload, expect=201)
        self.assertEqual(created["name"], "Mega Navigation")
        self.assertEqual(created["items_count"], 8)  # 1 top + 2 columns + 4 column links + 1 standalone = 8

        top_item = created["items"][0]
        self.assertEqual(top_item["title"], "Products")
        self.assertTrue(top_item["is_mega"])
        self.assertEqual(top_item["badge"], "Hot")
        self.assertEqual(top_item["promo_image"], "https://example.com/banner.jpg")
        self.assertEqual(top_item["promo_title"], "Mega Sale")
        self.assertEqual(top_item["promo_subtitle"], "Up to 50% Off")
        self.assertEqual(top_item["promo_url"], "/collections/sale")
        self.assertEqual(len(top_item["children"]), 2)

        wine_col = top_item["children"][0]
        self.assertEqual(wine_col["title"], "Wine")
        self.assertEqual(wine_col["badge"], "Popular")
        self.assertEqual(len(wine_col["children"]), 2)
        self.assertEqual(wine_col["children"][0]["title"], "Red Wine")
        self.assertEqual(wine_col["children"][1]["title"], "White Wine")
        # Level 3 items have no children
        self.assertEqual(wine_col["children"][0]["children"], [])

        # Retrieve via GET /<id>/
        detail_url = f"{self.URL}{created['id']}/"
        fetched = self.ok("get", detail_url)
        self.assertEqual(fetched["items_count"], 8)
        self.assertTrue(fetched["items"][0]["is_mega"])
        self.assertEqual(fetched["items"][0]["children"][0]["children"][0]["title"], "Red Wine")

        # Update via PUT with modified Level 3 items
        payload["items"][0]["children"][0]["children"].append({"title": "Rose Wine", "url": "/collections/rose"})
        updated = self.ok("put", detail_url, payload)
        self.assertEqual(updated["items_count"], 9)
        self.assertEqual(len(updated["items"][0]["children"][0]["children"]), 3)

    def test_menu_rejects_nesting_deeper_than_three_levels(self):
        payload = {
            "name": "Too Deep",
            "items": [
                {
                    "title": "Level 1",
                    "url": "/l1",
                    "children": [
                        {
                            "title": "Level 2",
                            "url": "/l2",
                            "children": [
                                {
                                    "title": "Level 3",
                                    "url": "/l3",
                                    "children": [
                                        {"title": "Level 4", "url": "/l4"}
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        res = self.client.post(self.URL, payload, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("items", res.data)

    def test_model_clean_rejects_nesting_deeper_than_three_levels(self):
        from django.core.exceptions import ValidationError as DjangoValidationError
        from apps.themes.models import Menu, MenuItem
        from apps.tenancy.models import Store

        store = Store.objects.get(store_name="Menu Store")
        menu = Menu.objects.create(store=store, name="Depth Check", handle="depth-check")
        l1 = MenuItem.objects.create(menu=menu, title="L1")
        l2 = MenuItem.objects.create(menu=menu, parent=l1, title="L2")
        l3 = MenuItem.objects.create(menu=menu, parent=l2, title="L3")
        l4 = MenuItem(menu=menu, parent=l3, title="L4")

        with self.assertRaises(DjangoValidationError):
            l4.clean()

    def test_mega_menu_field_validation(self):
        # Badge too long (> 30 chars)
        res = self.client.post(self.URL, {
            "name": "Badge Test",
            "items": [{"title": "Item", "url": "/", "badge": "A" * 31}],
        }, format="json")
        self.assertEqual(res.status_code, 400)

        # Unsafe promo image
        res = self.client.post(self.URL, {
            "name": "Promo Image Test",
            "items": [{"title": "Item", "url": "/", "promo_image": "javascript:alert(1)"}],
        }, format="json")
        self.assertEqual(res.status_code, 400)

        # Unsafe promo destination url
        res = self.client.post(self.URL, {
            "name": "Promo URL Test",
            "items": [{"title": "Item", "url": "/", "promo_url": "data:text/html,bad"}],
        }, format="json")
        self.assertEqual(res.status_code, 400)

    def test_backward_compatibility_with_flat_and_two_level_menus(self):
        # 1. Flat menu (no children, no mega fields)
        flat = self.ok("post", self.URL, {
            "name": "Simple Flat",
            "items": [{"title": "Home", "url": "/"}, {"title": "Contact", "url": "/pages/contact"}],
        }, expect=201)
        self.assertEqual(flat["items_count"], 2)
        self.assertFalse(flat["items"][0]["is_mega"])
        self.assertEqual(flat["items"][0]["badge"], "")
        self.assertEqual(flat["items"][0]["children"], [])

        # 2. 2-level menu without mega fields
        nested = self.ok("post", self.URL, {
            "name": "Standard Nested",
            "items": [
                {
                    "title": "Shop",
                    "url": "/collections/all",
                    "children": [{"title": "New Arrivals", "url": "/collections/new"}],
                }
            ],
        }, expect=201)
        self.assertEqual(nested["items_count"], 2)
        self.assertFalse(nested["items"][0]["is_mega"])
        self.assertEqual(nested["items"][0]["children"][0]["children"], [])


class ProductPageStockUpgradeTest(StoreClientMixin, APITestCase):
    """Stores get the product page that respects options which keep selling, unless they edited their own copy."""

    PATH = "sections/main-product.liquid"
    PREVIOUS_HASH = "f2e87477d18719a79f68bff217b82ce6415d3fbbefed5ebfea20a3b6f6c65a31"
    PREVIOUS_HASH_CRLF = "3c8b55dc638fb076f8c8e49a63c993ddeea59e6708c9c0eed86a63743cd8e1ae"

    def setUp(self):
        self.token, self.store = self.sign_up("pp-upgrade@example.com", "Product Page Upgrade Store")

    def theme(self):
        from apps.tenancy.models import Store

        from .services import get_store_theme

        return get_store_theme(Store.objects.get(store_id=self.store["storeId"])).theme

    def previous_starter_file(self):
        # The real revision-44 file can no longer be rebuilt from today's starter (revisions 52-54 rewrote it), so a stand-in
        # "old" copy is registered for the run: this checks how upgrades behave, the real hashes are checked below.
        import hashlib
        from unittest import mock

        from . import services

        text = services.starter_files()[self.PATH].replace("</section>", "<!-- older copy --></section>", 1)
        self.assertNotEqual(text, services.starter_files()[self.PATH])
        hashes = {hashlib.sha256(t.encode("utf-8")).hexdigest() for t in (text, text.replace("\n", "\r\n"))}
        patcher = mock.patch.dict(services.STARTER_UPGRADES, {self.PATH: services.STARTER_UPGRADES[self.PATH] | hashes})
        patcher.start()
        self.addCleanup(patcher.stop)
        return text

    def store_copy(self, theme, content):
        theme.files.filter(file_path=self.PATH).update(content=content, file_size=len(content.encode("utf-8")))
        theme.theme_data = {**theme.theme_data, "starter_revision": 43}
        theme.save()

    def stored(self, theme):
        return theme.files.get(file_path=self.PATH).content

    def test_the_file_stores_had_before_is_registered_as_never_edited(self):
        from .services import STARTER_UPGRADES, STARTER_REVISION

        self.assertLessEqual({self.PREVIOUS_HASH, self.PREVIOUS_HASH_CRLF}, STARTER_UPGRADES[self.PATH])
        self.assertGreaterEqual(STARTER_REVISION, 44)

    def test_a_store_with_the_untouched_old_file_gets_the_new_one(self):
        from . import services

        for newline in ("\n", "\r\n"):
            with self.subTest(newline=repr(newline)):
                theme = self.theme()
                self.store_copy(theme, self.previous_starter_file().replace("\n", newline))
                services._upgrade_untouched_starter_files(theme)
                self.assertEqual(self.stored(theme), services.starter_files()[self.PATH])
                self.assertIn('data-variant-continue="{% if variant.continue_selling or variant.tracked == false %}true{% else %}false{% endif %}"', self.stored(theme))

    def test_a_store_that_edited_its_copy_keeps_it(self):
        from . import services

        theme = self.theme()
        edited = self.previous_starter_file() + "\n<!-- my own change -->\n"
        self.store_copy(theme, edited)
        services._upgrade_untouched_starter_files(theme)
        self.assertEqual(self.stored(theme), edited)
        theme.refresh_from_db()
        self.assertEqual(theme.theme_data["starter_revision"], services.STARTER_REVISION)  # checked once, left alone


class SocialClickLayoutUpgradeTest(StoreClientMixin, APITestCase):
    """Stores get the layout whose script also counts clicks on social links, unless they edited their own copy."""

    PATH = "layout/theme.liquid"
    PREVIOUS_HASH = "0987e907fc71ca53d60862620a3adea06198870982b9895bfdf748b15594f7e3"
    PREVIOUS_HASH_CRLF = "0dc7cbfbf4afbd1e07d916c8a0da1c239d2a28ac51595beee81fb86630c35524"

    # (new text, the text it replaced): undoing it gives the layout stores had before
    CHANGES = (
        ('''    // Clicks on links to other sites: the server keeps only the social network found in the address (facebook, instagram...).
    function sendClick(e) {
      var a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
      if (!a || !/^https?:$/.test(a.protocol) || a.hostname === location.hostname) return;
      navigator.sendBeacon("{{ shop.url }}/_track", new Blob([JSON.stringify({ type: "social_click", url: a.href })], { type: "text/plain;charset=UTF-8" }));
    }
    document.addEventListener("click", sendClick);
    document.addEventListener("auxclick", sendClick);
''', ''''''),
    )

    def setUp(self):
        self.token, self.store = self.sign_up("click-upgrade@example.com", "Click Upgrade Store")

    def theme(self):
        from apps.tenancy.models import Store

        from .services import get_store_theme

        return get_store_theme(Store.objects.get(store_id=self.store["storeId"])).theme

    def previous_starter_file(self):
        from .services import starter_files

        text = starter_files()[self.PATH]
        for new, old in self.CHANGES:
            self.assertEqual(text.count(new), 1, new[:60])
            text = text.replace(new, old)
        return text

    def store_copy(self, theme, content):
        theme.files.filter(file_path=self.PATH).update(content=content, file_size=len(content.encode("utf-8")))
        theme.theme_data = {**theme.theme_data, "starter_revision": 49}
        theme.save()

    def stored(self, theme):
        return theme.files.get(file_path=self.PATH).content

    def test_the_layout_stores_had_before_is_registered_as_never_edited_and_the_new_one_is_not(self):
        import hashlib

        from .services import STARTER_REVISION, STARTER_UPGRADES, starter_files

        self.assertLessEqual({self.PREVIOUS_HASH, self.PREVIOUS_HASH_CRLF}, STARTER_UPGRADES[self.PATH])
        self.assertGreaterEqual(STARTER_REVISION, 50)
        current = starter_files()[self.PATH]
        for text in (current, current.replace("\n", "\r\n")):
            self.assertNotIn(hashlib.sha256(text.encode("utf-8")).hexdigest(), STARTER_UPGRADES[self.PATH])

    def test_a_store_with_the_untouched_old_layout_gets_the_click_listener(self):
        from . import services

        for newline in ("\n", "\r\n"):
            with self.subTest(newline=repr(newline)):
                theme = self.theme()
                self.store_copy(theme, self.previous_starter_file().replace("\n", newline))
                self.assertNotIn("social_click", self.stored(theme))
                services._upgrade_untouched_starter_files(theme)
                self.assertEqual(self.stored(theme), services.starter_files()[self.PATH])
                self.assertIn('type: "social_click"', self.stored(theme))

    def test_a_store_that_edited_its_layout_keeps_it(self):
        from . import services

        theme = self.theme()
        edited = self.previous_starter_file() + "\n<!-- my own change -->\n"
        self.store_copy(theme, edited)
        services._upgrade_untouched_starter_files(theme)
        self.assertEqual(self.stored(theme), edited)
        theme.refresh_from_db()
        self.assertEqual(theme.theme_data["starter_revision"], services.STARTER_REVISION)  # checked once, left alone


class ProductSEOTest(StoreClientMixin, APITestCase):
    """Product SEO templates: title, meta description, canonical URL, alt text, and JSON-LD structured data."""

    def setUp(self):
        self.token, self.store = self.sign_up("productseo@example.com", "SEO Store")

    def page(self, path, query=""):
        suffix = f"?{query}" if query else ""
        r = self.client.get(f"/s/{self.store['storeSlug']}/{path}{suffix}")
        self.assertEqual(r.status_code, 200, f"GET {path} failed: {r.status_code}")
        return r.content.decode("utf-8")

    def test_product_with_custom_seo_title_and_description(self):
        product = self.ok("post", "/api/v1/catalog/products/", {
            "title": "Canvas Shoes",
            "sku": "CS-1",
            "status": "active",
            "price": "89.99",
            "continue_selling": True,
            "vendor": "ShoeCo",
            "description": "<p>Comfortable shoes for daily walking.</p>",
            "seo_title": "Best Canvas Walking Shoes",
            "seo_description": "Shop our top-rated canvas shoes with all-day comfort and durable soles.",
            "images": [
                "/media/shoe1.jpg",
                "/media/shoe2.jpg",
            ],
        }, expect=201)

        html = self.page(f"products/{product['id']}")

        # 1. Title tag uses seo_title and includes shop name without duplicate
        self.assertIn("<title>Best Canvas Walking Shoes · SEO Store</title>", html)

        # 2. Meta description is rendered with custom seo_description
        self.assertIn(
            '<meta name="description" content="Shop our top-rated canvas shoes with all-day comfort and durable soles.">',
            html,
        )

        # 3. Canonical URL is present and clean
        self.assertIn(f'<link rel="canonical" href="http://testserver/s/{self.store["storeSlug"]}/products/{product["id"]}">', html)

        # 4. Canonical URL ignores tracking query parameters
        html_with_params = self.page(f"products/{product['id']}", query="utm_source=google&ref=campaign")
        self.assertIn(f'<link rel="canonical" href="http://testserver/s/{self.store["storeSlug"]}/products/{product["id"]}">', html_with_params)
        self.assertNotIn("utm_source", html_with_params.split("<link rel=\"canonical\"")[1].split(">")[0])

        # 5. Image alt attributes
        self.assertIn('alt="Canvas Shoes"', html)
        self.assertIn('alt="Canvas Shoes - Image 1"', html)
        self.assertIn('alt="Canvas Shoes - Image 2"', html)

        # 6. Schema.org Product JSON-LD structured data
        self.assertIn('<script type="application/ld+json">', html)
        json_match = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL)
        self.assertIsNotNone(json_match)
        data = json.loads(json_match.group(1))

        self.assertEqual(data["@context"], "https://schema.org")
        self.assertEqual(data["@type"], "Product")
        self.assertEqual(data["name"], "Canvas Shoes")
        self.assertEqual(data["description"], "Shop our top-rated canvas shoes with all-day comfort and durable soles.")
        self.assertEqual(data["brand"]["name"], "ShoeCo")
        self.assertEqual(data["image"], [
            "http://testserver/media/shoe1.jpg",
            "http://testserver/media/shoe2.jpg",
        ])
        self.assertEqual(data["offers"]["@type"], "Offer")
        self.assertEqual(data["offers"]["price"], "89.99")
        self.assertEqual(data["offers"]["priceCurrency"], "INR")
        self.assertEqual(data["offers"]["availability"], "https://schema.org/InStock")

    def test_product_seo_fallbacks_when_seo_fields_empty(self):
        product = self.ok("post", "/api/v1/catalog/products/", {
            "title": "Minimal Watch",
            "sku": "MW-1",
            "status": "active",
            "price": "149.00",
            "description": "<p>A sleek watch with a <b>leather strap</b>.<br>Water resistant to 50m.</p>",
            "seo_title": "",
            "seo_description": "",
        }, expect=201)

        html = self.page(f"products/{product['id']}")

        # Title falls back to product.title
        self.assertIn("<title>Minimal Watch · SEO Store</title>", html)

        # Meta description falls back to HTML-stripped product.description
        self.assertIn(
            '<meta name="description" content="A sleek watch with a leather strap. Water resistant to 50m.">',
            html,
        )

        # JSON-LD description also strips HTML tags
        json_match = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL)
        self.assertIsNotNone(json_match)
        data = json.loads(json_match.group(1))
        self.assertEqual(data["description"], "A sleek watch with a leather strap. Water resistant to 50m.")
        # No inventory rows = not tracked, so it is never sold out
        self.assertEqual(data["offers"]["availability"], "https://schema.org/InStock")

    def test_product_title_not_duplicated_if_brand_already_included(self):
        product = self.ok("post", "/api/v1/catalog/products/", {
            "title": "Backpack",
            "sku": "BP-1",
            "status": "active",
            "price": "49.00",
            "seo_title": "Backpack · SEO Store",
        }, expect=201)

        html = self.page(f"products/{product['id']}")
        # Store name should not be appended a second time
        self.assertIn("<title>Backpack · SEO Store</title>", html)
        self.assertNotIn("SEO Store · SEO Store", html)



class StorefrontSitemapAndRobotsTest(StoreClientMixin, APITestCase):
    def setUp(self):
        import datetime
        from django.utils import timezone
        from apps.tenancy.models import Store
        from apps.themes.models import BlogPost
        self.datetime = datetime
        self.timezone = timezone
        self.Store = Store
        self.BlogPost = BlogPost
        self.token, self.store = self.sign_up("sitemap_test@example.com", "Sitemap Store")
        self.store_obj = Store.objects.get(pk=self.store["storeId"])

    def test_storefront_sitemap_includes_active_products_and_excludes_draft_and_archived(self):
        p_active = self.ok("post", "/api/v1/catalog/products/", {
            "title": "Active Product",
            "sku": "ACT-1",
            "status": "active",
            "price": "29.99",
        }, expect=201)

        p_draft = self.ok("post", "/api/v1/catalog/products/", {
            "title": "Draft Product",
            "sku": "DRF-1",
            "status": "draft",
            "price": "19.99",
        }, expect=201)

        p_archived = self.ok("post", "/api/v1/catalog/products/", {
            "title": "Archived Product",
            "sku": "ARC-1",
            "status": "archived",
            "price": "39.99",
        }, expect=201)

        r = self.client.get(f"/s/{self.store['storeSlug']}/sitemap.xml")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("Content-Type"), "application/xml; charset=utf-8")

        content = r.content.decode("utf-8")
        self.assertIn('xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"', content)
        self.assertIn(f"/s/{self.store['storeSlug']}/", content)
        self.assertIn(f"/s/{self.store['storeSlug']}/collections/all", content)
        self.assertIn(f"/s/{self.store['storeSlug']}/products/{p_active['id']}", content)
        self.assertNotIn(f"/s/{self.store['storeSlug']}/products/{p_draft['id']}", content)
        self.assertNotIn(f"/s/{self.store['storeSlug']}/products/{p_archived['id']}", content)

    def test_storefront_sitemap_includes_active_collections_and_excludes_draft(self):
        c_active = self.ok("post", "/api/v1/catalog/collections/", {
            "name": "Summer Collection",
            "status": "active",
        }, expect=201)

        c_draft = self.ok("post", "/api/v1/catalog/collections/", {
            "name": "Winter Draft Collection",
            "status": "draft",
        }, expect=201)

        r = self.client.get(f"/s/{self.store['storeSlug']}/sitemap.xml")
        self.assertEqual(r.status_code, 200)
        content = r.content.decode("utf-8")
        self.assertIn(f"/s/{self.store['storeSlug']}/collections/{c_active['handle']}", content)
        self.assertNotIn(f"/s/{self.store['storeSlug']}/collections/{c_draft['handle']}", content)

    def test_storefront_sitemap_includes_visible_pages_and_excludes_hidden(self):
        page_vis = self.ok("post", "/api/v1/themes/pages/", {
            "title": "About Our Team",
            "body": "<p>About us content</p>",
            "status": "visible",
        }, expect=201)

        page_hid = self.ok("post", "/api/v1/themes/pages/", {
            "title": "Secret Hidden Page",
            "body": "<p>Hidden content</p>",
            "status": "hidden",
        }, expect=201)

        r = self.client.get(f"/s/{self.store['storeSlug']}/sitemap.xml")
        self.assertEqual(r.status_code, 200)
        content = r.content.decode("utf-8")
        self.assertIn(f"/s/{self.store['storeSlug']}/pages/{page_vis['handle']}", content)
        self.assertNotIn(f"/s/{self.store['storeSlug']}/pages/{page_hid['handle']}", content)

    def test_storefront_sitemap_includes_published_posts_and_excludes_draft_and_future(self):
        now = self.timezone.now()
        post_pub = self.BlogPost.objects.create(
            store=self.store_obj,
            title="Published Post",
            handle="published-post",
            status=self.BlogPost.PUBLISHED,
            published_at=now - self.datetime.timedelta(days=1),
        )
        post_draft = self.BlogPost.objects.create(
            store=self.store_obj,
            title="Draft Post",
            handle="draft-post",
            status=self.BlogPost.DRAFT,
            published_at=now,
        )
        post_future = self.BlogPost.objects.create(
            store=self.store_obj,
            title="Future Post",
            handle="future-post",
            status=self.BlogPost.PUBLISHED,
            published_at=now + self.datetime.timedelta(days=5),
        )

        r = self.client.get(f"/s/{self.store['storeSlug']}/sitemap.xml")
        self.assertEqual(r.status_code, 200)
        content = r.content.decode("utf-8")
        self.assertIn(f"/s/{self.store['storeSlug']}/blog", content)
        self.assertIn(f"/s/{self.store['storeSlug']}/blog/{post_pub.handle}", content)
        self.assertNotIn(f"/s/{self.store['storeSlug']}/blog/{post_draft.handle}", content)
        self.assertNotIn(f"/s/{self.store['storeSlug']}/blog/{post_future.handle}", content)

    def test_storefront_sitemap_and_robots_return_404_for_closed_or_invalid_store(self):
        r_invalid_sitemap = self.client.get("/s/non-existent-store-slug/sitemap.xml")
        self.assertEqual(r_invalid_sitemap.status_code, 404)

        r_invalid_robots = self.client.get("/s/non-existent-store-slug/robots.txt")
        self.assertEqual(r_invalid_robots.status_code, 404)

        # Close store
        self.Store.objects.filter(pk=self.store["storeId"]).update(status="closed")

        r_closed_sitemap = self.client.get(f"/s/{self.store['storeSlug']}/sitemap.xml")
        self.assertEqual(r_closed_sitemap.status_code, 404)

        r_closed_robots = self.client.get(f"/s/{self.store['storeSlug']}/robots.txt")
        self.assertEqual(r_closed_robots.status_code, 404)

    def test_storefront_robots_txt_contains_correct_rules_and_sitemap(self):
        r = self.client.get(f"/s/{self.store['storeSlug']}/robots.txt")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("Content-Type"), "text/plain; charset=utf-8")

        content = r.content.decode("utf-8")
        self.assertIn("User-agent: *", content)
        self.assertIn(f"Disallow: /s/{self.store['storeSlug']}/cart/", content)
        self.assertIn(f"Disallow: /s/{self.store['storeSlug']}/checkout/", content)
        self.assertIn(f"Disallow: /s/{self.store['storeSlug']}/account/", content)
        self.assertIn(f"Disallow: /s/{self.store['storeSlug']}/wishlist/", content)
        self.assertIn(f"Sitemap: http://testserver/s/{self.store['storeSlug']}/sitemap.xml", content)

    def test_existing_storefront_routes_continue_working(self):
        r_home = self.client.get(f"/s/{self.store['storeSlug']}/")
        self.assertEqual(r_home.status_code, 200)

        r_col = self.client.get(f"/s/{self.store['storeSlug']}/collections/all")
        self.assertEqual(r_col.status_code, 200)



class StorefrontMegaMenuRenderingTest(StoreClientMixin, APITestCase):
    """Storefront Liquid template rendering tests for Mega Menu and dropdowns."""

    def setUp(self):
        from apps.tenancy.models import Store
        from .models import Menu, MenuItem

        self.Store = Store
        self.Menu = Menu
        self.MenuItem = MenuItem
        self.token, self.store = self.sign_up("megamenu_shopper@example.com", "MegaMenu Store")
        self.store_obj = Store.objects.get(pk=self.store["storeId"])
        self.slug = self.store["storeSlug"]

    def nav_html(self, html):
        start = html.index('<nav class="site-header__nav"')
        end = html.index('</nav>', start) + len('</nav>')
        return html[start:end]

    def test_mega_menu_3_level_hierarchy_with_promo_and_badges(self):
        menu = self.Menu.objects.create(store=self.store_obj, name="Main menu", handle="main-menu")
        l1 = self.MenuItem.objects.create(
            menu=menu, title="Catalog", url="/collections/all", order=0,
            is_mega=True, badge="New",
            promo_image="https://example.com/promo.jpg",
            promo_title="Special Offer",
            promo_subtitle="Get 20% off all beverages",
            promo_url="/collections/all",
        )
        col1 = self.MenuItem.objects.create(
            menu=menu, parent=l1, title="Beverages", url="/collections/all", order=0, badge="Hot",
        )
        self.MenuItem.objects.create(
            menu=menu, parent=col1, title="Coffee", url="/pages/coffee", order=0,
        )
        self.MenuItem.objects.create(
            menu=menu, parent=col1, title="Tea", url="/pages/tea", order=1, badge="Best",
        )

        col2 = self.MenuItem.objects.create(
            menu=menu, parent=l1, title="Bakery", url="/collections/all", order=1,
        )
        self.MenuItem.objects.create(
            menu=menu, parent=col2, title="Croissant", url="/pages/croissant", order=0,
        )

        r = self.client.get(f"/s/{self.slug}/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode("utf-8")
        nav = self.nav_html(html)

        # Mega menu container in navbar
        self.assertIn("site-header__item--mega", nav)
        self.assertIn('class="mega-menu"', nav)
        # Level 1 badge
        self.assertIn('Catalog<span class="menu-badge">New</span>', nav)
        # Level 2 columns
        self.assertIn("Beverages", nav)
        self.assertIn('Beverages<span class="menu-badge">Hot</span>', nav)
        self.assertIn("Bakery", nav)
        # Level 3 links
        self.assertIn("Coffee", nav)
        self.assertIn('Tea<span class="menu-badge">Best</span>', nav)
        self.assertIn("Croissant", nav)
        # Promo banner in navbar
        self.assertIn("https://example.com/promo.jpg", nav)
        self.assertIn("Special Offer", nav)
        self.assertIn("Get 20% off all beverages", nav)
        self.assertIn(f"/s/{self.slug}/collections/all", nav)

    def test_mega_menu_without_promo_banner(self):
        menu = self.Menu.objects.create(store=self.store_obj, name="Main menu", handle="main-menu")
        l1 = self.MenuItem.objects.create(
            menu=menu, title="Shop", url="/collections/all", order=0, is_mega=True,
        )
        col = self.MenuItem.objects.create(
            menu=menu, parent=l1, title="Clothing", url="/collections/all", order=0,
        )
        self.MenuItem.objects.create(
            menu=menu, parent=col, title="Jackets", url="/pages/jackets", order=0,
        )

        r = self.client.get(f"/s/{self.slug}/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode("utf-8")
        nav = self.nav_html(html)

        self.assertIn("site-header__item--mega", nav)
        self.assertIn("Clothing", nav)
        self.assertIn("Jackets", nav)
        self.assertNotIn("mega-menu__promo", nav)

    def test_an_item_with_children_opens_the_full_width_panel_even_when_not_marked_mega(self):
        menu = self.Menu.objects.create(store=self.store_obj, name="Main menu", handle="main-menu")
        l1 = self.MenuItem.objects.create(
            menu=menu, title="Explore", url="/collections/all", order=0, is_mega=False,
        )
        col = self.MenuItem.objects.create(
            menu=menu, parent=l1, title="Guides", url="/pages/guides", order=0, badge="Pro",
        )
        self.MenuItem.objects.create(
            menu=menu, parent=col, title="Quickstart", url="/pages/quickstart", order=0,
        )

        r = self.client.get(f"/s/{self.slug}/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode("utf-8")
        nav = self.nav_html(html)

        self.assertIn("site-header__item--mega", nav)
        self.assertIn('class="mega-menu"', nav)
        # the small dropdown no longer exists
        self.assertNotIn("site-header__item--has-dropdown", nav)
        self.assertNotIn('class="site-nav__dropdown"', nav)
        self.assertIn("Guides", nav)
        self.assertIn('Guides<span class="menu-badge">Pro</span>', nav)
        self.assertIn("Quickstart", nav)

    def test_flat_item_when_no_children_and_not_mega(self):
        menu = self.Menu.objects.create(store=self.store_obj, name="Main menu", handle="main-menu")
        self.MenuItem.objects.create(
            menu=menu, title="Contact", url="/pages/contact", order=0, is_mega=False,
        )

        r = self.client.get(f"/s/{self.slug}/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode("utf-8")
        nav = self.nav_html(html)

        self.assertNotIn("site-header__item--mega", nav)
        self.assertNotIn("site-header__disclosure", nav.split("site-header__item--more")[0])
        self.assertIn(f'href="/s/{self.slug}/pages/contact">Contact</a>', nav)

    def test_all_five_presets_render_mega_menu_properly(self):
        menu = self.Menu.objects.create(store=self.store_obj, name="Main menu", handle="main-menu")
        l1 = self.MenuItem.objects.create(
            menu=menu, title="Menu", url="/collections/all", order=0,
            is_mega=True, badge="VIP",
            promo_image="https://example.com/banner.png",
            promo_title="Banner Title",
            promo_subtitle="Banner Subtitle",
            promo_url="/collections/all",
        )
        col = self.MenuItem.objects.create(
            menu=menu, parent=l1, title="Category 1", url="/collections/all", order=0, badge="Hot",
        )
        self.MenuItem.objects.create(
            menu=menu, parent=col, title="SubItem 1", url="/pages/p1", order=0,
        )

        presets = ["bold", "boutique", "classic", "fresh", "minimal"]
        for preset_name in presets:
            created = self.ok("post", "/api/v1/themes/library/", {"source": preset_name}, expect=201)
            self.ok("post", f"/api/v1/themes/library/{created['id']}/publish/")
            r = self.client.get(f"/s/{self.slug}/")
            self.assertEqual(r.status_code, 200, f"Preset {preset_name} failed with status {r.status_code}")
            html = r.content.decode("utf-8")
            nav = self.nav_html(html)
            self.assertIn("site-header__item--mega", nav, f"Preset {preset_name} missing mega menu class")
            self.assertIn("Category 1", nav, f"Preset {preset_name} missing column title")
            self.assertIn("SubItem 1", nav, f"Preset {preset_name} missing subitem")
            self.assertIn("Banner Title", nav, f"Preset {preset_name} missing promo title")

    def test_mega_menu_desktop_and_mobile_responsive_markup_and_aria(self):
        menu = self.Menu.objects.create(store=self.store_obj, name="Main menu", handle="main-menu")
        l1 = self.MenuItem.objects.create(
            menu=menu, title="Shop", url="/collections/all", order=0,
            is_mega=True, badge="Popular",
            promo_image="https://example.com/promo.png",
            promo_title="Exclusive Summer Drop",
            promo_subtitle="Explore now",
            promo_url="/collections/all",
        )
        col = self.MenuItem.objects.create(
            menu=menu, parent=l1, title="Menswear", url="/collections/all", order=0,
        )
        self.MenuItem.objects.create(
            menu=menu, parent=col, title="Jackets", url="/pages/jackets", order=0, badge="Warm",
        )

        r = self.client.get(f"/s/{self.slug}/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode("utf-8")
        nav = self.nav_html(html)

        # Disclosure button (desktop) and drill-down button (mobile)
        self.assertIn('class="site-header__disclosure"', nav)
        self.assertIn('aria-expanded="false"', nav)
        self.assertIn('aria-controls="mega-menu-', nav)
        self.assertIn("data-nav-drill", nav)

        # Mega menu container ARIA
        self.assertIn('role="region"', nav)
        self.assertIn('aria-label="Shop"', nav)

        # Level 2 column toggle for the mobile accordion setting
        self.assertIn('class="mega-menu__column-header"', nav)
        self.assertIn('class="mega-menu__col-toggle"', nav)
        self.assertIn('class="mega-menu__toggle-icon"', nav)
        self.assertIn('aria-controls="mega-col-', nav)

        # Level 3 links container
        self.assertIn('id="mega-col-', nav)
        self.assertIn('class="mega-menu__sublinks"', nav)
        self.assertIn('Jackets<span class="menu-badge">Warm</span>', nav)

        # Scripts and styles for Desktop & Mobile UX
        self.assertIn("initMegaMenus", html)
        self.assertIn("leaveTimer", html)
        self.assertIn("Escape", html)
        self.assertIn("@media (max-width: 768px)", html)

    def test_an_item_that_was_a_small_dropdown_gets_the_same_panel_markup_and_aria(self):
        menu = self.Menu.objects.create(store=self.store_obj, name="Main menu", handle="main-menu")
        l1 = self.MenuItem.objects.create(
            menu=menu, title="Pages", url="/collections/all", order=0, is_mega=False,
        )
        col = self.MenuItem.objects.create(
            menu=menu, parent=l1, title="Company", url="/pages/company", order=0,
        )
        self.MenuItem.objects.create(
            menu=menu, parent=col, title="About Us", url="/pages/about", order=0,
        )

        r = self.client.get(f"/s/{self.slug}/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode("utf-8")
        nav = self.nav_html(html)

        self.assertIn('aria-controls="mega-menu-', nav)
        self.assertIn('class="site-header__disclosure"', nav)
        self.assertIn('role="region"', nav)
        for old in ("role=\"menu\"", "role=\"menuitem\"", "site-nav__dropdown", "site-header__mobile-toggle"):
            self.assertNotIn(old, nav)


class HeaderMegaMenuLookTest(StoreClientMixin, APITestCase):
    """The mega menu's look (column headings, links, Shop All, colours, spacing) and how it reaches the storefront."""

    PREVIOUS_STARTER_HEADER_HASHES = (
        "5f762425519ec74471a1758ad9ba87509887f7562738958bc9c54491f19ce3d4",  # LF
        "184f6cf6357886e396e971eb1a96548a17f80878557c68e3c281b2c866a32e03",  # CRLF
    )
    # the revision 50 header (before the ShopNow-style navigation): its own hashes are registered too
    REV50_STARTER_HEADER_HASHES = (
        "a6c7d0813a1059fc6f28fc7d7ca18503bf6e03c6a6827beb6209e39ce3ec460a",  # LF
        "3fcc073642dc12db2a015c663e9381733a822cb9d4c819f470292ccaf8910011",  # CRLF
    )

    def setUp(self):
        self.token, self.store = self.sign_up("megalook@example.com", "Mega Look Store")
        self.slug = self.store["storeSlug"]

    def wine_menu(self, columns=None):
        """Wine > Red Wine > Shiraz, Pinot Noir ... in Admin > Menus, the only place the columns and links are built."""
        columns = columns or [("Red Wine", "/collections/all", ["Shiraz", "Pinot Noir"])]
        self.ok("post", "/api/v1/themes/menus/", {"name": "Main menu", "items": [{
            "title": "Wine", "url": "/collections/all", "is_mega": True,
            "children": [
                {"title": title, "url": url, "children": [{"title": link, "url": "/collections/all"} for link in links]}
                for title, url, links in columns
            ],
        }]}, expect=201)

    def home(self):
        r = self.client.get(f"/s/{self.slug}/")
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def nav(self):
        html = self.home()
        start = html.index('<nav class="site-header__nav"')
        return html[start:html.index("</nav>", start)]

    def header_tag(self):
        html = self.home()
        start = html.index('<header class="site-header"')
        return html[start:html.index(">", html.index("style=", start))]

    def header(self):
        return self.ok("get", "/api/v1/themes/sections/")["header"]

    def save_settings(self, **changes):
        values = self.header()["values"]
        values["settings"].update(changes)
        self.ok("patch", "/api/v1/themes/settings/", {"sections": {"header": values}, "client": "test"})

    # ---------------------------------------------------------------- Shop All

    def test_a_column_ends_with_an_underlined_shop_all_link_to_the_columns_url(self):
        self.wine_menu()
        nav = self.nav()
        self.assertIn('<div class="mega-menu__column-title">Red Wine</div>', nav)  # the heading is text
        self.assertNotIn("mega-menu__column-title--link", nav)
        self.assertEqual(nav.count('class="mega-menu__shop-all"'), 1)
        self.assertIn(f'<a class="mega-menu__shop-all" href="/s/{self.slug}/collections/all">Shop All</a>', nav)
        self.assertLess(nav.index("Pinot Noir"), nav.index("mega-menu__shop-all"))  # at the end of the column
        self.assertLess(nav.index("mega-menu__column-title"), nav.index("Shiraz"))  # the heading comes first

    def test_each_column_has_its_own_shop_all_after_its_own_links(self):
        self.wine_menu([("Red Wine", "/collections/all", ["Shiraz"]), ("White Wine", "/collections/all", ["Riesling"])])
        nav = self.nav()
        self.assertEqual(nav.count('class="mega-menu__shop-all"'), 2)
        first, second = nav.index("mega-menu__shop-all"), nav.rindex("mega-menu__shop-all")
        self.assertLess(nav.index("Shiraz"), first)
        self.assertLess(first, nav.index("White Wine"))
        self.assertLess(nav.index("Riesling"), second)

    def test_a_column_without_a_link_has_no_shop_all(self):
        self.wine_menu([("Other Wine", "", ["Port"]), ("Red Wine", "/collections/all", ["Shiraz"])])
        nav = self.nav()
        self.assertIn('<div class="mega-menu__column-title">Other Wine</div>', nav)
        self.assertIn("Port", nav)
        self.assertEqual(nav.count('class="mega-menu__shop-all"'), 1)  # only Red Wine's

    def test_turning_shop_all_off_never_makes_the_desktop_heading_a_link(self):
        self.wine_menu()
        self.save_settings(mega_show_shop_all=False)
        nav = self.nav()
        self.assertNotIn('class="mega-menu__shop-all"', nav)  # no Shop All at the end of the column on desktop
        self.assertIn('<div class="mega-menu__column-title">Red Wine</div>', nav)
        self.assertNotIn("mega-menu__column-title--link", nav)
        self.assertNotIn('<a class="mega-menu__column-title', nav)
        # the column's link is kept for the mobile accordion, hidden on desktop
        self.assertIn('class="mega-menu__shop-all mega-menu__shop-all--mobile-only"', nav)
        self.assertIn("a.mega-menu__shop-all--mobile-only {\n      display: none;", self.home())

    def test_shop_all_text_can_be_changed(self):
        self.wine_menu()
        self.save_settings(mega_shop_all_label="Browse all")
        nav = self.nav()
        self.assertIn('href="/s/%s/collections/all">Browse all</a>' % self.slug, nav)
        self.assertNotIn(">Shop All</a>", nav)

    def test_the_columns_shop_all_is_inside_its_column_and_only_shows_on_mobile_in_an_opened_accordion_column(self):
        self.wine_menu()
        html = self.home()
        nav = self.nav()
        column = nav[nav.index('<div class="mega-menu__column'):]
        self.assertLess(column.index('<ul class="mega-menu__sublinks"'), column.index("mega-menu__shop-all"))
        mobile = html[html.index("@media (max-width: 768px)"):]
        self.assertIn("body .site-header__nav a.mega-menu__shop-all {\n        display: none;", mobile)
        self.assertIn(
            'body .site-header[data-mobile-accordion="true"] .mega-menu__column--has-sublinks.is-col-open a.mega-menu__shop-all', mobile)

    # ---------------------------------------------------------------- the look

    def test_the_look_settings_are_in_the_header_with_the_designed_defaults(self):
        settings = self.header()["spec"]["settings"]
        expected = {
            "mega_bg": "", "mega_heading_color": "", "mega_heading_size": 15, "mega_link_color": "",
            "mega_link_size": 17, "mega_row_spacing": 38, "mega_column_gap": 80, "mega_padding_left": 0,
            "mega_show_shop_all": True, "mega_shop_all_label": "Shop All",
        }
        for key, default in expected.items():
            self.assertIn(key, settings)
            self.assertEqual(settings[key]["default"], default, key)
            self.assertEqual(self.header()["values"]["settings"][key], default, key)
        self.assertEqual(settings["mega_heading_size"]["unit"], "px")

    def test_the_default_look_reaches_the_storefront(self):
        self.wine_menu()
        tag = self.header_tag()
        for declaration in (
            "--mega-heading-size:15px;", "--mega-link-size:17px;", "--mega-row:38px;", "--mega-gap:80px;",
            "--mega-pad-left:0px;",
        ):
            self.assertIn(declaration, tag)
        # no colour is fixed: the CSS falls back to the theme's accent and text colours
        self.assertNotIn("--mega-heading-color", tag)
        self.assertNotIn("--mega-link-color", tag)
        html = self.home()
        self.assertIn("color: var(--mega-heading-color, var(--accent));", html)
        self.assertIn("color: var(--mega-link-color, var(--text));", html)
        self.assertIn("font-size: var(--mega-heading-size, 15px);", html)
        self.assertIn("min-height: var(--mega-row, 38px);", html)
        self.assertIn("text-transform: none;", html[html.index(".mega-menu__column-title {"):])
        self.assertIn("text-decoration: underline;", html[html.index("a.mega-menu__shop-all"):])

    def test_the_panel_follows_the_header_colour_until_it_has_its_own(self):
        self.wine_menu()
        self.save_settings(background="")
        self.assertNotIn("--mega-bg", self.header_tag())  # no header colour: the page background
        self.assertIn("background: var(--mega-bg, var(--bg));", self.home())
        self.save_settings(background="#A31736")
        self.assertIn("--mega-bg:#A31736;", self.header_tag())
        self.save_settings(mega_bg="#112233")
        self.assertIn("--mega-bg:#112233;", self.header_tag())
        self.assertIn("background:#A31736;", self.header_tag())  # the header itself keeps its colour

    def test_link_colour_follows_the_header_text_colour_until_it_has_its_own(self):
        self.wine_menu()
        self.save_settings(text_color="#F5E6C8")
        self.assertIn("--mega-link-color:#F5E6C8;", self.header_tag())
        self.save_settings(mega_link_color="#FFFFFF")  # what the burgundy store sets
        tag = self.header_tag()
        self.assertIn("--mega-link-color:#FFFFFF;", tag)
        self.assertNotIn("--mega-link-color:#F5E6C8;", tag)
        self.save_settings(mega_link_color="", text_color="")
        self.assertNotIn("--mega-link-color", self.header_tag())

    def test_heading_colour_is_only_set_when_the_owner_picks_one(self):
        self.wine_menu()
        self.assertNotIn("--mega-heading-color", self.header_tag())
        self.save_settings(mega_heading_color="#C9A35A")  # what the burgundy store sets
        self.assertIn("--mega-heading-color:#C9A35A;", self.header_tag())

    def test_changed_look_settings_reach_the_storefront_right_away(self):
        self.wine_menu()
        self.save_settings(
            mega_heading_color="#FFD700", mega_heading_size=18, mega_link_color="#EEEEEE", mega_link_size=20,
            mega_row_spacing=44, mega_column_gap=120, mega_padding_left=75,
        )
        tag = self.header_tag()
        for declaration in (
            "--mega-heading-color:#FFD700;", "--mega-heading-size:18px;", "--mega-link-color:#EEEEEE;",
            "--mega-link-size:20px;", "--mega-row:44px;", "--mega-gap:120px;", "--mega-pad-left:75px;",
        ):
            self.assertIn(declaration, tag)

    def test_look_settings_are_kept_inside_their_ranges_and_colours_stay_safe(self):
        self.wine_menu()
        self.save_settings(mega_heading_size=500, mega_row_spacing=1, mega_link_color="red;}</style><script>x</script>")
        tag = self.header_tag()
        self.assertIn("--mega-heading-size:28px;", tag)
        self.assertIn("--mega-row:20px;", tag)
        self.assertNotIn("--mega-link-color", tag)  # not a colour: back to the default (none)
        self.assertNotIn("<script>x", self.home())

    def test_the_look_settings_follow_edit_code(self):
        import json

        files = self.ok("get", "/api/v1/themes/files/")["files"]
        code = next(f["content"] for f in files if f["path"] == "sections/header.liquid")
        markup, schema_text, _ = sync.split(code)
        schema = json.loads(schema_text)
        for setting in schema["settings"]:
            if setting["id"] == "mega_shop_all_label":
                setting["default"] = "View all"
            if setting["id"] == "mega_link_size":
                setting["max"] = 40
        start = code.index("{% schema %}")
        changed = code[:start] + "{% schema %}\n" + json.dumps(schema, indent=2) + "\n{% endschema %}\n"
        r = self.client.post("/api/v1/themes/files/", {"path": "sections/header.liquid", "content": changed}, format="json")
        self.assertEqual(r.status_code, 200, r.content[:300])
        settings = self.header()["spec"]["settings"]
        self.assertEqual(settings["mega_shop_all_label"]["default"], "View all")
        self.assertEqual(settings["mega_link_size"]["max"], 40)
        # ... and a value changed in the sidebar is read by the same markup
        self.wine_menu()
        self.save_settings(mega_shop_all_label="See everything", mega_link_size=30)
        self.assertIn("--mega-link-size:30px;", self.header_tag())
        self.assertIn(">See everything</a>", self.nav())

    def test_the_columns_still_come_only_from_admin_menus(self):
        spec = self.header()["spec"]
        self.assertEqual(sorted(spec["blocks"]), ["announcement", "cart", "link", "search", "wishlist"])
        self.assertEqual(sorted(spec["blocks"]["link"]["settings"]), ["label", "url"])  # no columns or links in the blocks

    # ---------------------------------------------------------------- existing stores and the snippet

    def test_the_previous_header_is_registered_for_the_upgrade_and_the_new_one_is_not(self):
        import hashlib

        from .services import STARTER_UPGRADES, starter_files

        for digest in self.PREVIOUS_STARTER_HEADER_HASHES + self.REV50_STARTER_HEADER_HASHES:
            self.assertIn(digest, STARTER_UPGRADES["sections/header.liquid"])
        current = starter_files()["sections/header.liquid"]
        for text in (current, current.replace("\n", "\r\n")):
            self.assertNotIn(hashlib.sha256(text.encode("utf-8")).hexdigest(), STARTER_UPGRADES["sections/header.liquid"])

    def test_the_new_header_snippet_is_a_known_original_so_untouched_themes_get_the_section(self):
        from .services import starter_files

        snippet = starter_files()["snippets/header.liquid"]
        self.assertTrue(sync.is_original_snippet("header", snippet))
        self.assertTrue(sync.is_original_snippet("header", snippet.replace("\n", "\r\n")))
        self.assertGreaterEqual(sync.ORIGINALS_REVISION, 13)

    def test_an_edited_header_is_not_replaced_by_the_upgrade(self):
        from . import services
        from apps.tenancy.models import Store

        store_theme = services.get_store_theme(Store.objects.get(store_id=self.store["storeId"]))
        theme = store_theme.theme
        edited = edited_header()
        theme.files.filter(file_path="sections/header.liquid").update(content=edited, file_size=len(edited.encode("utf-8")))
        theme.theme_data = {**theme.theme_data, "starter_revision": 48}
        theme.save()
        services._upgrade_untouched_starter_files(theme)
        self.assertEqual(theme.files.get(file_path="sections/header.liquid").content, edited)

    def test_the_header_snippet_has_the_same_column_layout_and_shop_all(self):
        from .services import starter_files

        snippet = starter_files()["snippets/header.liquid"]
        self.assertIn('href="{{ col.url }}">{{ mega_shop_all_label }}</a>', snippet)
        self.assertIn('<a class="mega-menu__item-shop-all" href="{{ href }}">{{ mega_shop_all_label }} {{ label }}</a>', snippet)
        self.assertNotIn("column-title--link", snippet)
        self.assertIn("font-size: var(--mega-heading-size, 15px);", snippet)
        self.assertIn("--mega-bg:{{ settings.header_bg }};", snippet)
        # the snippet's mega menu CSS is the section's
        section = starter_files()["sections/header.liquid"]
        start, end = "/* Mega menu container", "/* Promo banner */"
        self.assertEqual(section[section.index(start):section.index(end)], snippet[snippet.index(start):snippet.index(end)])


class HeaderShopNowNavigationTest(StoreClientMixin, APITestCase):
    """
    The header navigation modelled on ShopNow (Shopify Horizon): full-width panels with plain-text column headings,
    "Shop All", a disclosure button per item, "More", and on mobile a left drawer with a drill-down. Admin > Menus stays
    the source of the menu; the header's look settings stay in step with Edit code. The script is not run here: its
    behaviour is checked through the markup it works on and the code it ships.
    """

    def setUp(self):
        self.token, self.store = self.sign_up("shopnow-nav@example.com", "ShopNow Nav Store")
        self.slug = self.store["storeSlug"]

    # ---------------------------------------------------------------- helpers

    def menu(self, items):
        """Admin > Menus: the navbar menu is created once, then its items are replaced."""
        if getattr(self, "menu_id", None):
            self.ok("patch", f"/api/v1/themes/menus/{self.menu_id}/", {"items": items})
        else:
            self.menu_id = self.ok("post", "/api/v1/themes/menus/", {"name": "Main menu", "items": items}, expect=201)["id"]

    def wine_menu(self, url="/collections/all", columns=None):
        columns = columns or [("Red Wine", "/collections/all", ["Shiraz", "Pinot Noir"]), ("Bubbles", "", ["Prosecco"])]
        self.menu([{
            "title": "Wine", "url": url,
            "children": [
                {"title": title, "url": col_url, "children": [{"title": link, "url": "/collections/all"} for link in links]}
                for title, col_url, links in columns
            ],
        }])

    def home(self):
        r = self.client.get(f"/s/{self.slug}/")
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def nav(self):
        html = self.home()
        start = html.index('<nav class="site-header__nav"')
        return html[start:html.index("</nav>", start)]

    def header_html(self):
        html = self.home()
        return html[html.index('<header class="site-header"'):html.index("</header>")]

    def header_tag(self):
        html = self.home()
        start = html.index('<header class="site-header"')
        return html[start:html.index(">", html.index("style=", start))]

    def header(self):
        return self.ok("get", "/api/v1/themes/sections/")["header"]

    def save_settings(self, **changes):
        values = self.header()["values"]
        values["settings"].update(changes)
        self.ok("patch", "/api/v1/themes/settings/", {"sections": {"header": values}, "client": "test"})

    def header_code(self):
        files = self.ok("get", "/api/v1/themes/files/")["files"]
        return next(f["content"] for f in files if f["path"] == "sections/header.liquid")

    # ---------------------------------------------------------------- desktop: headings, Shop All

    def test_column_headings_are_plain_text_whatever_the_shop_all_setting(self):
        self.wine_menu()
        for show in (True, False):
            self.save_settings(mega_show_shop_all=show)
            nav = self.nav()
            self.assertIn('<div class="mega-menu__column-title">Red Wine</div>', nav, show)
            self.assertIn('<div class="mega-menu__column-title">Bubbles</div>', nav, show)
            self.assertNotIn('<a class="mega-menu__column-title', nav, show)
            self.assertNotIn("mega-menu__column-title--link", nav, show)
        self.assertNotIn("a.mega-menu__column-title", self.home())  # and no CSS for a heading link

    def test_the_columns_link_is_shop_all_at_the_end_of_the_column(self):
        self.wine_menu()
        nav = self.nav()
        self.assertEqual(nav.count('class="mega-menu__shop-all"'), 1)  # Bubbles has no link
        self.assertIn(f'<a class="mega-menu__shop-all" href="/s/{self.slug}/collections/all">Shop All</a>', nav)
        self.assertLess(nav.index("Pinot Noir"), nav.index('class="mega-menu__shop-all"'))
        self.assertLess(nav.index('class="mega-menu__shop-all"'), nav.index("Prosecco"))  # before the next column

    def test_an_item_with_a_real_link_shows_shop_all_item_at_the_top_of_its_panel(self):
        self.wine_menu()
        nav = self.nav()
        top = f'<a class="mega-menu__item-shop-all" href="/s/{self.slug}/collections/all">Shop All Wine</a>'
        self.assertIn(top, nav)
        self.assertEqual(nav.count('class="mega-menu__item-shop-all"'), 1)
        self.assertLess(nav.index(top), nav.index('<div class="mega-menu__columns">'))  # at the top
        self.save_settings(mega_shop_all_label="Browse all")
        self.assertIn(">Browse all Wine</a>", self.nav())

    def test_the_item_shop_all_does_not_depend_on_the_column_shop_all_setting(self):
        self.wine_menu()
        self.save_settings(mega_show_shop_all=False)
        self.assertIn(">Shop All Wine</a>", self.nav())

    def test_an_item_without_a_real_link_has_no_shop_all(self):
        for url in ("#", ""):
            self.wine_menu(url=url)
            nav = self.nav()
            self.assertNotIn("mega-menu__item-shop-all", nav, repr(url))
            self.assertNotIn("Shop All Wine", nav, repr(url))
            self.assertIn("Red Wine", nav, repr(url))  # its panel is still there

    # ---------------------------------------------------------------- desktop: panels and the disclosure button

    def test_every_item_with_children_opens_the_full_width_panel(self):
        self.menu([
            {"title": "Plain", "url": "/collections/all", "is_mega": False,
             "children": [{"title": "Group", "url": "/collections/all", "children": [{"title": "Leaf", "url": "/collections/all"}]}]},
            {"title": "Marked", "url": "/collections/all", "is_mega": True,
             "children": [{"title": "Other group", "url": "/collections/all", "children": []}]},
        ])
        nav = self.nav()
        self.assertEqual(nav.count("site-header__item--mega"), 2)
        self.assertEqual(nav.count('class="mega-menu"'), 2)
        for gone in ("site-nav__dropdown", "has-dropdown", 'role="menu"'):
            self.assertNotIn(gone, nav)

    def test_an_item_without_children_is_a_plain_link(self):
        self.menu([{"title": "Contact", "url": "/pages/contact"}])
        nav = self.nav()
        self.assertNotIn('class="mega-menu"', nav)
        self.assertNotIn("data-nav-item data-nav-label", nav)
        self.assertIn(f'<a class="site-header__link" href="/s/{self.slug}/pages/contact">Contact</a>', nav)

    def test_each_item_has_a_disclosure_button_wired_to_its_panel(self):
        self.wine_menu()
        nav = self.nav()
        match = re.search(
            r'<button class="site-header__disclosure" type="button" aria-expanded="false" aria-controls="(mega-menu-[^"]+)"', nav)
        self.assertIsNotNone(match)
        self.assertIn(f'<div class="mega-menu" id="{match.group(1)}" role="region"', nav)
        self.assertIn('aria-label="Wine menu"', nav)
        # next to the item's own link, which stays a link
        self.assertLess(nav.index(f'<a class="site-header__link" href="/s/{self.slug}/collections/all">Wine'),
                        nav.index("site-header__disclosure"))

    def test_an_item_without_a_link_is_itself_the_disclosure_button(self):
        self.wine_menu(url="#")
        nav = self.nav()
        self.assertIn('<button class="site-header__link site-header__disclosure site-header__disclosure--label" type="button" '
                      'aria-expanded="false" aria-controls="mega-menu-', nav)
        self.assertNotIn('<a class="site-header__link" href="#"', nav)

    def test_the_panel_follows_the_button_so_tab_walks_its_links(self):
        self.wine_menu()
        nav = self.nav()
        self.assertLess(nav.index("site-header__disclosure"), nav.index('<div class="mega-menu"'))
        self.assertLess(nav.index('<div class="mega-menu"'), nav.index("Shiraz"))
        self.assertNotIn('tabindex="-1"', nav)
        self.assertIn('<a class="mega-menu__sublink"', nav)

    def test_closed_panels_are_inert(self):
        self.wine_menu()
        nav = self.nav()
        panels = re.findall(r'<div class="mega-menu"[^>]*>', nav)
        self.assertEqual(len(panels), 1)
        self.assertTrue(all(" inert" in tag for tag in panels), panels)
        self.assertRegex(nav, r'<div class="site-nav__more"[^>]* inert>')  # More starts closed too
        script = self.home()
        self.assertIn("panel.removeAttribute('inert')", script)  # opening makes it live
        self.assertIn("panel.setAttribute('inert', '')", script)  # closing makes it inert again

    def test_keyboard_and_pointer_behaviour_is_in_the_script(self):
        script = self.home()
        for needle in (
            "toggle.addEventListener('click'",      # Enter and Space press the button, which toggles
            "if (isOpen(item)) setOpen(item, false); else openItem(item, 'pinned');",
            "event.key !== 'Escape'",               # Esc closes ...
            "if (focused && toggle) toggle.focus();",  # ... and returns focus to the button
            "document.addEventListener('pointerdown'",  # click outside closes
            "item.leaveTimer",                      # hover has a short grace period
            "item.addEventListener('focusout'",     # Tab out of the item closes it
        ):
            self.assertIn(needle, script)
        self.assertIn("initMegaMenus", script)

    # ---------------------------------------------------------------- desktop: colours and reveal

    def test_the_top_level_hover_colour_defaults_to_the_heading_colour(self):
        self.wine_menu()
        setting = self.header()["spec"]["settings"]["mega_top_hover_color"]
        self.assertEqual(setting["default"], "")
        self.assertEqual(setting["type"], "color_optional")
        self.assertNotIn("--mega-top-hover", self.header_tag())
        html = self.home()
        self.assertIn("color: var(--mega-top-hover, var(--mega-heading-color, var(--accent)));", html)
        self.assertIn(".site-header__item.is-open > .site-header__link", html)  # hover and active

    def test_the_top_level_hover_colour_can_be_set(self):
        self.wine_menu()
        self.save_settings(mega_top_hover_color="#C9A35A")
        self.assertIn("--mega-top-hover:#C9A35A;", self.header_tag())

    def test_the_reveal_slides_for_360ms_and_fades_for_270ms_and_respects_reduced_motion(self):
        self.wine_menu()
        html = " ".join(self.home().split())
        self.assertIn("clip-path 0.36s cubic-bezier(0.4, 0, 0.2, 1)", html)
        self.assertIn("transform 0.36s cubic-bezier(0.4, 0, 0.2, 1)", html)
        self.assertIn("opacity 0.27s ease", html)
        self.assertIn("clip-path: inset(0 0 100% 0);", html)  # closed: clipped away
        start = html.index("@media (prefers-reduced-motion: reduce) { body .site-header__nav .mega-menu,")
        reduced = html[start:html.index("} }", start)]
        self.assertIn("transition: none !important;", reduced)
        self.assertIn("animation: none !important;", reduced)
        for selector in (".mega-menu", ".site-nav__more", ".site-header__drawer", ".site-header__backdrop"):
            self.assertIn(selector, reduced)

    def test_items_that_do_not_fit_move_into_more(self):
        self.menu([{"title": f"Item {n}", "url": "/collections/all",
                    "children": [{"title": "Group", "url": "/collections/all", "children": [{"title": "Leaf", "url": "/collections/all"}]}]}
                   for n in range(6)])
        nav = self.nav()
        self.assertEqual(nav.count("data-nav-top"), 6)
        self.assertIn('<div class="site-header__item site-header__item--more" data-nav-item data-nav-more hidden>', nav)
        self.assertIn("data-nav-more-panel", nav)
        self.assertIn(">More<", nav)
        script = self.home()
        self.assertIn("nav.scrollWidth > nav.clientWidth + 1", script)
        self.assertIn("tops[i].hidden = true;", script)  # from the last one back
        self.assertIn("ResizeObserver", script)
        # More lists the moved items with their column headings as text
        self.assertIn("'site-nav__more-title'", script)
        self.assertIn("flex-wrap: nowrap;", self.home())

    def test_more_is_never_shown_on_mobile_where_the_menu_is_a_drawer(self):
        self.wine_menu()
        mobile = self.home()
        mobile = mobile[mobile.index("@media (max-width: 768px)"):]
        self.assertIn("body .site-header__nav .site-header__item--more {\n        display: none !important;", mobile)

    # ---------------------------------------------------------------- mobile: header row

    def test_the_mobile_header_row_is_logo_menu_button_space_search_icons(self):
        self.wine_menu()
        header = self.header_html()
        self.assertLess(header.index('class="site-header__logo"'), header.index('class="site-header__hamburger"'))
        self.assertLess(header.index('class="site-header__hamburger"'), header.index('class="site-header__actions"'))
        for icon in ('data-header-search', 'class="site-header__wishlist"', 'class="site-header__cart"'):
            self.assertGreater(header.index(icon), header.index('class="site-header__actions"'))  # all in the icons group
        mobile = self.home()
        mobile = mobile[mobile.index("@media (max-width: 768px)"):]
        self.assertIn("flex-wrap: nowrap;", mobile)
        self.assertIn("margin-left: auto;", mobile[mobile.index(".site-header__actions"):])  # the space
        self.assertIn("body .site-header .site-header__search-block {\n        order: -1;", mobile)  # search before the icons

    def test_the_menu_button_is_a_labelled_disclosure_for_the_drawer(self):
        self.wine_menu()
        header = self.header_html()
        self.assertRegex(
            header,
            r'<button class="site-header__hamburger" type="button" aria-expanded="false" aria-controls="site-nav-[^"]+" '
            r'aria-label="Open menu" data-nav-open>')
        self.assertIn('id="site-nav-', header)

    def test_no_menu_button_without_the_navigation(self):
        self.save_settings(show_nav=False)
        header = self.header_html()
        self.assertNotIn("site-header__hamburger", header)
        self.assertNotIn("data-nav-drawer", header)

    def test_the_mobile_logo_height_and_menu_text_size_have_defaults_and_ranges(self):
        settings = self.header()["spec"]["settings"]
        self.assertEqual(settings["mobile_logo_height"]["default"], 42)
        self.assertEqual(settings["mobile_logo_height"]["unit"], "px")
        self.assertEqual(settings["mobile_menu_text_size"]["default"], 12)
        self.assertEqual(settings["mobile_menu_text_size"]["unit"], "px")
        tag = self.header_tag()
        self.assertIn("--logo-height-mobile:42px;", tag)
        self.assertIn("--mobile-menu-size:12px;", tag)
        html = self.home()
        self.assertIn("height: var(--logo-height-mobile, 42px) !important;", html)
        self.assertIn("font-size: var(--mobile-menu-size, 12px);", html)

    def test_the_mobile_logo_height_and_menu_text_size_reach_the_storefront_and_stay_in_range(self):
        self.save_settings(mobile_logo_height=60, mobile_menu_text_size=15)
        tag = self.header_tag()
        self.assertIn("--logo-height-mobile:60px;", tag)
        self.assertIn("--mobile-menu-size:15px;", tag)
        self.save_settings(mobile_logo_height=900, mobile_menu_text_size=1)
        tag = self.header_tag()
        self.assertIn("--logo-height-mobile:96px;", tag)
        self.assertIn("--mobile-menu-size:10px;", tag)

    # ---------------------------------------------------------------- mobile: drawer

    def test_the_drawer_comes_from_the_left_over_a_backdrop(self):
        self.wine_menu()
        header = self.header_html()
        self.assertIn('<div class="site-header__backdrop" data-nav-backdrop hidden></div>', header)
        self.assertLess(header.index("data-nav-backdrop"), header.index("data-nav-drawer"))
        self.assertLess(header.index("data-nav-drawer"), header.index('<nav class="site-header__nav"'))  # the menu is inside
        html = " ".join(self.home().split())
        self.assertIn("transform: translateX(-100%);", html)  # closed: off to the left
        self.assertIn("body .site-header__drawer { position: fixed; top: 0; bottom: 0; left: 0;", html)

    def test_the_drawer_script_locks_scroll_traps_focus_and_closes_on_backdrop_and_esc(self):
        script = self.home()
        for needle in (
            "html.style.overflow = 'hidden';",                       # scroll lock ...
            "html.style.overflow = scrollState[0];",                 # ... and its release
            "drawer.setAttribute('aria-modal', 'true');",
            "if (event.key !== 'Tab' || !drawerOpen) return;",       # focus trap
            "if (!root.contains(active)) { event.preventDefault(); first.focus(); }",
            "else if (event.shiftKey && active === first) { event.preventDefault(); last.focus(); }",
            "else if (!event.shiftKey && active === last) { event.preventDefault(); first.focus(); }",
            "backdrop.addEventListener('click', function () { closeDrawer(true); });",
            "if (drawerOpen) { event.preventDefault(); closeDrawer(true); return; }",  # Esc
            "if (restoreFocus && burger) burger.focus();",
        ):
            self.assertIn(needle, script)

    # ---------------------------------------------------------------- mobile: drill-down

    def test_a_top_item_opens_its_own_panel_with_back_arrow_centred_title_and_close(self):
        self.wine_menu()
        nav = self.nav()
        drill = re.search(r'<button class="site-header__drill" type="button" aria-expanded="false" aria-controls="(mega-menu-[^"]+)" data-nav-drill>Wine', nav)
        self.assertIsNotNone(drill)
        head = nav[nav.index('<div class="mega-menu__mobile-head">'):nav.index('<div class="page-width mega-menu__inner">')]
        self.assertLess(head.index("data-nav-back"), head.index('<span class="mega-menu__mobile-title">Wine</span>'))
        self.assertLess(head.index("mega-menu__mobile-title"), head.index("data-nav-close"))
        self.assertIn('aria-label="Back to main menu"', head)
        self.assertIn('aria-label="Close menu"', head)
        mobile = " ".join(self.home().split())
        mobile = mobile[mobile.index("@media (max-width: 768px)"):]
        self.assertIn("grid-template-columns: 52px 1fr 52px;", mobile)  # back | centred title | close
        self.assertIn("text-align: center;", mobile[mobile.index(".mega-menu__mobile-title"):])
        self.assertIn("drill.addEventListener('click'", self.home())

    def test_the_panel_lists_headings_with_their_links_then_shop_all(self):
        self.wine_menu()
        nav = self.nav()
        self.assertIn(f'<a class="mega-menu__column-link" href="/s/{self.slug}/collections/all">Red Wine</a>', nav)  # a heading link
        self.assertIn('<span class="mega-menu__column-link">Bubbles</span>', nav)  # no link: text
        self.assertLess(nav.index("Prosecco"), nav.index('class="mega-menu__item-shop-all-mobile"'))  # then Shop All
        self.assertIn(f'<a class="mega-menu__item-shop-all-mobile" href="/s/{self.slug}/collections/all">Shop All</a>', nav)
        self.assertEqual(nav.count('class="mega-menu__item-shop-all-mobile"'), 1)

    def test_the_accordion_setting_is_off_by_default(self):
        self.wine_menu()
        setting = self.header()["spec"]["settings"]["mobile_accordion"]
        self.assertIs(setting["default"], False)
        self.assertEqual(setting["type"], "checkbox")
        self.assertIn('data-mobile-accordion="false"', self.header_html())

    def test_with_the_accordion_on_headings_expand_and_collapse_their_links(self):
        self.wine_menu()
        self.save_settings(mobile_accordion=True)
        self.assertIn('data-mobile-accordion="true"', self.header_html())
        nav = self.nav()
        self.assertRegex(nav, r'<button class="mega-menu__col-toggle" type="button" aria-expanded="false" aria-controls="mega-col-[^"]+">Red Wine')
        self.assertEqual(nav.count('class="mega-menu__col-toggle"'), 2)  # one per column with links
        html = " ".join(self.home().split())
        # only the accordion setting turns the toggle on and collapses the links
        self.assertIn('body .site-header[data-mobile-accordion="true"] .mega-menu__col-toggle { display: flex;', html)
        self.assertIn(':not(.is-col-open) .mega-menu__sublinks', html)
        self.assertIn("column.classList.add('is-col-open')", html)

    # ---------------------------------------------------------------- Shop All colours

    def test_the_colour_settings_are_empty_by_default_and_follow_the_theme(self):
        self.wine_menu()
        settings = self.header()["spec"]["settings"]
        for key in ("mega_heading_color", "mega_top_hover_color", "mega_shop_all_color", "mega_shop_all_color_mobile"):
            self.assertEqual(settings[key]["default"], "", key)
            self.assertEqual(settings[key]["type"], "color_optional", key)
        tag = self.header_tag()
        for variable in ("--mega-heading-color", "--mega-top-hover", "--mega-shop-all", "--mega-shop-all-mobile"):
            self.assertNotIn(variable, tag)
        html = self.home()
        self.assertIn("color: var(--mega-shop-all, var(--mega-heading-color, var(--accent)));", html)
        self.assertIn("color: var(--mega-shop-all-mobile, var(--mega-heading-color, var(--accent)));", html)

    def test_the_colour_settings_reach_the_storefront_when_set(self):
        self.wine_menu()
        self.save_settings(mega_heading_color="#111111", mega_shop_all_color="#222222", mega_shop_all_color_mobile="#333333")
        tag = self.header_tag()
        self.assertIn("--mega-heading-color:#111111;", tag)
        self.assertIn("--mega-shop-all:#222222;", tag)
        self.assertIn("--mega-shop-all-mobile:#333333;", tag)

    def test_the_colour_settings_only_accept_colours(self):
        self.wine_menu()
        self.save_settings(mega_top_hover_color="red;}</style><script>x</script>", mega_shop_all_color="url(x)")
        tag = self.header_tag()
        self.assertNotIn("--mega-top-hover", tag)
        self.assertNotIn("--mega-shop-all", tag)
        self.assertNotIn("<script>x", self.home())

    # ---------------------------------------------------------------- menu data and Edit code

    def test_admin_menus_stays_the_source_of_the_columns_and_links(self):
        spec = self.header()["spec"]
        self.assertEqual(sorted(spec["blocks"]["link"]["settings"]), ["label", "url"])
        self.wine_menu()
        self.assertIn("Prosecco", self.nav())
        self.menu([{"title": "Spirits", "url": "/collections/all",
                    "children": [{"title": "Whisky", "url": "/collections/all", "children": [{"title": "Islay", "url": "/collections/all"}]}]}])
        nav = self.nav()
        self.assertIn("Islay", nav)
        self.assertNotIn("Prosecco", nav)

    def test_a_menu_link_block_still_picks_its_item_by_label(self):
        self.wine_menu()
        values = self.header()["values"]
        values["blocks"] = [{"type": "link", "settings": {"label": "Wine", "url": "/pages/wine"}},
                            {"type": "link", "settings": {"label": "Journal", "url": "/pages/journal"}}]
        self.ok("patch", "/api/v1/themes/settings/", {"sections": {"header": values}, "client": "test"})
        nav = self.nav()
        self.assertIn("Prosecco", nav)  # Wine's panel, from Admin > Menus
        self.assertIn(f'<a class="site-header__link" href="/s/{self.slug}/pages/journal">Journal</a>', nav)

    def test_the_new_settings_are_edited_in_code_and_the_sidebar_follows(self):
        code = self.header_code()
        markup, schema_text, _ = sync.split(code)
        schema = json.loads(schema_text)
        ids = {s["id"] for s in schema["settings"]}
        for key in ("mega_top_hover_color", "mega_shop_all_color", "mega_shop_all_color_mobile",
                    "mobile_logo_height", "mobile_menu_text_size", "mobile_accordion"):
            self.assertIn(key, ids)
        for setting in schema["settings"]:
            if setting["id"] == "mobile_logo_height":
                setting["default"] = 50
            if setting["id"] == "mobile_accordion":
                setting["default"] = True
        start = code.index("{% schema %}")
        changed = code[:start] + "{% schema %}\n" + json.dumps(schema, indent=2) + "\n{% endschema %}\n"
        r = self.client.post("/api/v1/themes/files/", {"path": "sections/header.liquid", "content": changed}, format="json")
        self.assertEqual(r.status_code, 200, r.content[:300])
        settings = self.header()["spec"]["settings"]
        self.assertEqual(settings["mobile_logo_height"]["default"], 50)
        self.assertIn("--logo-height-mobile:50px;", self.header_tag())
        self.assertIn('data-mobile-accordion="true"', self.header_html())

    def test_a_value_saved_in_the_sidebar_is_written_back_into_the_code(self):
        self.save_settings(mobile_logo_height=64, mobile_menu_text_size=14, mobile_accordion=True, mega_top_hover_color="#AA0000")
        schema = sync.parse(self.header_code())
        by_id = {s["id"]: s for s in schema["settings"]}
        self.assertEqual(by_id["mobile_logo_height"]["default"], 64)
        self.assertEqual(by_id["mobile_menu_text_size"]["default"], 14)
        self.assertIs(by_id["mobile_accordion"]["default"], True)
        self.assertEqual(by_id["mega_top_hover_color"]["default"], "#AA0000")

    # ---------------------------------------------------------------- upgrading existing stores

    def test_the_saved_revision_50_header_is_the_real_one_and_both_revision_50_files_are_registered_for_the_upgrade(self):
        from .services import STARTER_UPGRADES

        header = rev50_header()
        crlf = header.replace("\n", "\r\n")
        self.assertEqual(hashlib.sha256(header.encode("utf-8")).hexdigest(), "a6c7d0813a1059fc6f28fc7d7ca18503bf6e03c6a6827beb6209e39ce3ec460a")
        self.assertEqual(hashlib.sha256(crlf.encode("utf-8")).hexdigest(), "3fcc073642dc12db2a015c663e9381733a822cb9d4c819f470292ccaf8910011")
        for text in (header, crlf):
            self.assertIn(hashlib.sha256(text.encode("utf-8")).hexdigest(), STARTER_UPGRADES["sections/header.liquid"])
        # the revision 50 snippet: only its hashes are kept (nothing needs its text)
        for digest in ("8cfe7c6a3f81bc906f7d9c6c75be2113e9d109e041ce033605dcce93ad0042a7",
                       "6842a0fcc327395aa20f37fc0980fdb1e2f0f0a5075e3a7aeeda3b5348b2010b"):
            self.assertIn(digest, STARTER_UPGRADES["snippets/header.liquid"])
            self.assertIn(digest, sync.SYNCED_SECTIONS["header"]["originals"])  # themes still on it are checked again

    def test_the_new_header_and_snippet_are_not_registered_as_old(self):
        from .services import STARTER_UPGRADES, starter_files

        for path in ("sections/header.liquid", "snippets/header.liquid"):
            current = starter_files()[path]
            for text in (current, current.replace("\n", "\r\n")):
                self.assertNotIn(hashlib.sha256(text.encode("utf-8")).hexdigest(), STARTER_UPGRADES[path], path)
        snippet = starter_files()["snippets/header.liquid"]
        self.assertTrue(sync.is_original_snippet("header", snippet))
        self.assertTrue(sync.is_original_snippet("header", snippet.replace("\n", "\r\n")))
        self.assertGreaterEqual(sync.ORIGINALS_REVISION, 14)

    def test_the_mega_menu_panel_is_as_wide_as_the_header_so_columns_line_up_with_the_logo(self):
        import re

        from .services import STARTER_UPGRADES, starter_files

        # The platform widens every .page-width (rendering.SECTION_CSS), so the panel's own max-width: var(--page)
        # kept it 200px narrower than the header on wide screens and pushed the columns to the right of the logo.
        for path in ("sections/header.liquid", "snippets/header.liquid"):
            code = starter_files()[path]
            rule = code[code.index(".site-header__nav .mega-menu__inner {"):]
            rule = rule[:rule.index("}")]
            self.assertIsNone(re.search(r"^\s*max-width\s*:", rule, re.M), path)  # the comment may name it; a declaration may not
            self.assertIn('class="page-width mega-menu__inner"', code)
        # the revision 51 files (which had the cap) are registered, so untouched stores get the fix
        for digest in ("3c397975d7ccc807b322de2f895e9907043d6b97b3b53f18b5143588a36fbdfb",
                       "06abcf0322802d08414f520abb64c726e4c24c0c474c3ec7e34decb1a397474e"):
            self.assertIn(digest, STARTER_UPGRADES["sections/header.liquid"])
        for digest in ("d8989f6198f2593c81303fd80a9b64eecbd0c91cda3fd05fe5297f2c2c0205d3",
                       "fb57134df11b1de0f109858ee9209016634cf327f01ec87e3df2fe783225d2bb"):
            self.assertIn(digest, STARTER_UPGRADES["snippets/header.liquid"])
            self.assertIn(digest, sync.SYNCED_SECTIONS["header"]["originals"])

    def test_an_untouched_revision_50_header_is_upgraded_and_keeps_its_history(self):
        from apps.tenancy.models import Store

        from . import services

        store_theme = services.get_store_theme(Store.objects.get(store_id=self.store["storeId"]))
        theme = store_theme.theme
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=repr(newline)):
                old = rev50_header().replace("\n", newline)
                theme.files.filter(file_path="sections/header.liquid").update(content=old, file_size=len(old.encode("utf-8")))
                theme.theme_data = {**theme.theme_data, "starter_revision": 50}
                theme.save()
                services._upgrade_untouched_starter_files(theme)
                stored = theme.files.get(file_path="sections/header.liquid").content
                self.assertEqual(stored, services.starter_files()["sections/header.liquid"])
                self.assertIn("data-nav-drawer", stored)
                self.assertTrue(sync.history_of(theme, "header"))  # the replaced header can be restored
        html = self.home()
        self.assertIn('class="site-header__hamburger"', html)

    def test_an_edited_revision_50_header_stays_as_the_owner_left_it(self):
        from apps.tenancy.models import Store

        from . import services

        store_theme = services.get_store_theme(Store.objects.get(store_id=self.store["storeId"]))
        theme = store_theme.theme
        mine = rev50_header() + "\n<!-- my own change -->\n"
        theme.files.filter(file_path="sections/header.liquid").update(content=mine, file_size=len(mine.encode("utf-8")))
        theme.theme_data = {**theme.theme_data, "starter_revision": 50}
        theme.save()
        services._upgrade_untouched_starter_files(theme)
        self.assertEqual(theme.files.get(file_path="sections/header.liquid").content, mine)
        theme.refresh_from_db()
        self.assertEqual(theme.theme_data["starter_revision"], services.STARTER_REVISION)  # checked once, left alone

    def test_the_snippet_is_in_step_with_the_section(self):
        from .services import starter_files

        section, snippet = starter_files()["sections/header.liquid"], starter_files()["snippets/header.liquid"]
        start, end = "    body .site-header {\n      position: sticky;", "  </style>"
        css = section[section.index(start):section.index("</style>", section.index(start))].rstrip()
        self.assertEqual(css, snippet[snippet.index(start):snippet.index(end, snippet.index(start))].rstrip())
        script = lambda text: text[text.index("(function () {\n      var DESKTOP"):text.index("})();", text.index("var DESKTOP")) + 5]
        self.assertEqual(script(section), script(snippet))
        for needle in ("data-nav-drawer", "data-nav-more", "mega-menu__item-shop-all", "mega-menu__column-link", "inert"):
            self.assertIn(needle, snippet)
        self.assertNotIn("column-title--link", snippet)


class ThemePerformanceCacheTest(StoreClientMixin, APITestCase):
    """The per-request work that was cut: preparing a theme, loading its files, parsing its templates, the schema."""

    def setUp(self):
        from . import services

        services.clear_theme_caches()
        self.token, self.store = self.sign_up("perf@example.com", "Perf Store")

    def theme(self):
        from .models import StoreTheme

        return StoreTheme.objects.select_related("theme").get(store_id=self.store["storeId"], is_active=True)

    def prepares(self, call):
        from unittest import mock

        from . import services

        with mock.patch.object(services, "_prepare", wraps=services._prepare) as prepare:
            call()
        return prepare.call_count

    def test_theme_is_prepared_once_until_it_changes(self):
        self.ok("get", "/api/v1/themes/settings/")
        self.ok("get", "/api/v1/themes/settings/")  # settles anything the first preparation saved
        self.assertEqual(self.prepares(lambda: self.ok("get", "/api/v1/themes/settings/")), 0)

        # A file is saved: the next request prepares again
        file = self.theme().theme.files.get(file_path="layout/theme.liquid")
        file.content += "\n"
        file.file_size = len(file.content.encode("utf-8"))
        file.save(update_fields=["content", "file_size", "updated_at"])
        self.assertEqual(self.prepares(lambda: self.ok("get", "/api/v1/themes/settings/")), 1)

    def test_a_missing_starter_file_is_still_added_for_an_existing_store(self):
        self.ok("get", "/api/v1/themes/settings/")
        self.ok("get", "/api/v1/themes/settings/")
        theme = self.theme().theme
        theme.files.filter(file_path="templates/product.liquid").delete()
        self.ok("get", "/api/v1/themes/settings/")
        self.assertTrue(theme.files.filter(file_path="templates/product.liquid").exists())

    def test_settings_changes_prepare_again(self):
        self.ok("get", "/api/v1/themes/settings/")
        self.ok("get", "/api/v1/themes/settings/")
        self.ok("patch", "/api/v1/themes/settings/", {"settings": {}})
        self.ok("get", "/api/v1/themes/settings/")  # settles
        self.assertEqual(self.prepares(lambda: self.ok("get", "/api/v1/themes/settings/")), 0)

    def test_files_are_loaded_once_and_follow_edits(self):
        from .services import theme_files

        self.ok("get", "/api/v1/themes/settings/")
        store_theme = self.theme()
        first = theme_files(store_theme)
        first["layout/theme.liquid"] = "mutated by a caller"
        again = theme_files(store_theme)
        self.assertNotEqual(again["layout/theme.liquid"], "mutated by a caller")

        with self.assertNumQueries(1):  # only the check that nothing changed
            theme_files(store_theme)

        file = store_theme.theme.files.get(file_path="snippets/product-card.liquid")
        file.content = file.content + "<!-- edited -->"
        file.file_size = len(file.content.encode("utf-8"))
        file.save(update_fields=["content", "file_size", "updated_at"])
        self.assertTrue(theme_files(store_theme)["snippets/product-card.liquid"].endswith("<!-- edited -->"))

        store_theme.theme.files.filter(file_path="snippets/product-card.liquid").delete()
        self.assertNotIn("snippets/product-card.liquid", theme_files(store_theme))

    def test_files_of_two_stores_stay_apart(self):
        from .services import theme_files

        self.ok("get", "/api/v1/themes/settings/")
        mine = self.theme()
        edited = mine.theme.files.get(file_path="layout/theme.liquid")
        edited.content = "STORE ONE"
        edited.file_size = 9
        edited.save(update_fields=["content", "file_size", "updated_at"])
        self.assertEqual(theme_files(mine)["layout/theme.liquid"], "STORE ONE")

        token, other = self.sign_up("perf2@example.com", "Other Store")
        self.ok("get", "/api/v1/themes/settings/")
        from .models import StoreTheme

        theirs = StoreTheme.objects.select_related("theme").get(store_id=other["storeId"], is_active=True)
        self.assertNotEqual(theirs.theme_id, mine.theme_id)
        self.assertNotEqual(theme_files(theirs)["layout/theme.liquid"], "STORE ONE")

    def test_environment_is_reused_for_the_same_files_and_rebuilt_for_changed_ones(self):
        from . import rendering

        files = {"snippets/a.liquid": "A {{ 1 | money }}", "layout/theme.liquid": "{% render 'a' %}"}
        env = rendering._environment(files, "/s/x", "$")
        self.assertIs(env, rendering._environment(dict(files), "/s/x", "$"))
        self.assertIsNot(env, rendering._environment(files, "/s/x", "â‚¹"))  # the money filter is bound to the currency
        self.assertIsNot(env, rendering._environment(files, "/s/y", "$"))
        changed = {**files, "snippets/a.liquid": "B {{ 1 | money }}"}
        edited = rendering._environment(changed, "/s/x", "$")
        self.assertIsNot(env, edited)
        self.assertEqual(edited.get_template("layout/theme.liquid").render(), "B $1.00")
        self.assertEqual(env.get_template("layout/theme.liquid").render(), "A $1.00")

    def test_preview_shows_an_edited_snippet_straight_away(self):
        settings = self.ok("get", "/api/v1/themes/settings/")
        first = self.client.post("/api/v1/themes/preview/?page=index", settings, format="json")
        self.assertEqual(first.status_code, 200)
        file = self.theme().theme.files.get(file_path="layout/theme.liquid")
        file.content = file.content.replace("</body>", "<!-- perf-marker --></body>", 1)
        file.file_size = len(file.content.encode("utf-8"))
        file.save(update_fields=["content", "file_size", "updated_at"])
        second = self.client.post("/api/v1/themes/preview/?page=index", settings, format="json")
        self.assertIn("perf-marker", second.json()["html"])
        self.assertNotIn("perf-marker", first.json()["html"])

    def test_schema_is_cacheable_and_revalidates(self):
        first = self.client.get("/api/v1/themes/schema/")
        self.assertEqual(first.status_code, 200)
        etag = first["ETag"]
        self.assertIn("max-age", first["Cache-Control"])
        again = self.client.get("/api/v1/themes/schema/", HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(again.status_code, 304)
        stale = self.client.get("/api/v1/themes/schema/", HTTP_IF_NONE_MATCH='"other"')
        self.assertEqual(stale.status_code, 200)
        self.assertEqual(stale.json(), first.json())

    def test_a_snippet_rendered_in_a_loop_is_parsed_once(self):
        from unittest import mock

        from liquid import Environment

        from . import rendering

        files = {
            "snippets/card.liquid": "[{{ item }}]",
            "layout/theme.liquid": "{% for i in (1..5) %}{% render 'card', item: i %}{% endfor %}",
        }
        env = rendering._environment(files, "/s/loop", "$")
        with mock.patch.object(Environment, "from_string", autospec=True, side_effect=Environment.from_string) as parse:
            self.assertEqual(env.get_template("layout/theme.liquid").render(), "[1][2][3][4][5]")
            self.assertEqual(env.get_template("layout/theme.liquid").render(), "[1][2][3][4][5]")
        self.assertEqual(parse.call_count, 2)  # the layout and the card, once each


class StorefrontCustomerTokenTest(APITestCase):
    """The customer cookie / ct token stop working once the password changes."""

    def test_token_dies_when_the_password_changes(self):
        from django.contrib.auth import get_user_model
        from django.test import RequestFactory
        from apps.tenancy.models import Store

        from .views import CUSTOMER_LINK_PARAM, customer_link_token, get_storefront_customer

        user = get_user_model().objects.create_user(email="tok@example.com", password="Old-pass-12345")
        store = Store.objects.create(store_name="Tok", store_slug="tok-shop", status="active")
        token = customer_link_token(store, user)
        request = RequestFactory().get("/", {CUSTOMER_LINK_PARAM: token})

        self.assertEqual(get_storefront_customer(store, request)[1], user)

        user.set_password("New-pass-12345")
        user.save()
        self.assertEqual(get_storefront_customer(store, request), (None, None))



class OutOfStockAddToCartTest(StoreClientMixin, APITestCase):
    """Out-of-stock Add to Cart button handling, Edit Theme settings, and precedence."""

    def setUp(self):
        from apps.catalog.models import ProductVariant
        from apps.inventory.models import InventoryItem

        self.ProductVariant = ProductVariant
        self.InventoryItem = InventoryItem
        self.token, self.store = self.sign_up("stocktest@example.com", "Stock Test Store")
        self.slug = self.store["storeSlug"]
        self.base = f"/s/{self.slug}"
        self.location = self.ok("post", "/api/v1/inventory/locations/", {"name": "Warehouse"}, expect=201)["id"]

        self.in_stock_prod = self.ok("post", "/api/v1/catalog/products/", {
            "title": "In Stock Shirt", "sku": "SHIRT-IN", "status": "active", "price": "25.00",
            "location": self.location, "quantity": 15
        }, expect=201)

        self.out_stock_prod = self.ok("post", "/api/v1/catalog/products/", {
            "title": "Out Stock Shirt", "sku": "SHIRT-OUT", "status": "active", "price": "25.00",
            "location": self.location, "quantity": 0
        }, expect=201)

    def test_in_stock_product_renders_enabled_add_to_cart_button(self):
        res = self.client.get(f"{self.base}/products/{self.in_stock_prod['id']}")
        self.assertEqual(res.status_code, 200)
        html = res.content.decode()
        self.assertIn('data-add', html)
        self.assertIn('>Add to cart</button>', html)
        # Ensure it does not have disabled attribute on the button
        self.assertRegex(html, r'<button[^>]*data-add(?![^>]*disabled)[^>]*>')

    def test_out_of_stock_product_renders_disabled_button_with_sold_out(self):
        res = self.client.get(f"{self.base}/products/{self.out_stock_prod['id']}")
        self.assertEqual(res.status_code, 200)
        html = res.content.decode()
        self.assertIn('data-add', html)
        self.assertIn('>Sold out</button>', html)
        self.assertIn('disabled', html)
        self.assertIn('data-sold-out="Sold out"', html)

    def test_theme_setting_custom_sold_out_label_applies_to_out_of_stock_product(self):
        # Update theme setting: sold_out_button_label
        self.ok("patch", "/api/v1/themes/settings/", {
            "settings": {"sold_out_button_label": "Currently Unavailable"}
        })
        res = self.client.get(f"{self.base}/products/{self.out_stock_prod['id']}")
        self.assertEqual(res.status_code, 200)
        html = res.content.decode()
        self.assertIn('>Currently Unavailable</button>', html)
        self.assertIn('data-sold-out="Currently Unavailable"', html)
        self.assertIn('data-sold-out-label="Currently Unavailable"', html)

    def test_section_override_precedence_over_global_theme_setting(self):
        # Global setting says "Theme Sold Out"
        # Section override says "Section Sold Out"
        data = self.ok("get", "/api/v1/themes/settings/")
        prod_template = data["templates"]["product"]
        prod_template["sections"]["main"]["settings"]["sold_out_label"] = "Section Sold Out"
        self.ok("patch", "/api/v1/themes/settings/", {
            "settings": {"sold_out_button_label": "Theme Sold Out"},
            "templates": {
                "product": prod_template
            }
        })
        res = self.client.get(f"{self.base}/products/{self.out_stock_prod['id']}")
        self.assertEqual(res.status_code, 200)
        html = res.content.decode()
        # Section override wins!
        self.assertIn('>Section Sold Out</button>', html)
        self.assertIn('data-sold-out="Section Sold Out"', html)
        self.assertIn('data-sold-out-label="Section Sold Out"', html)

    def test_continue_selling_variant_remains_available_even_with_zero_stock(self):
        var_id = self.out_stock_prod["variants"][0]["id"]
        self.ProductVariant.objects.filter(pk=var_id).update(continue_selling=True)
        res = self.client.get(f"{self.base}/products/{self.out_stock_prod['id']}")
        self.assertEqual(res.status_code, 200)
        html = res.content.decode()
        # Variant that continues selling is not sold out
        self.assertIn('>Add to cart</button>', html)
        self.assertRegex(html, r'<button[^>]*data-add(?![^>]*disabled)[^>]*>')

    def test_disable_out_of_stock_turned_off_in_theme_settings(self):
        self.ok("patch", "/api/v1/themes/settings/", {
            "settings": {"disable_out_of_stock_add_to_cart": False}
        })
        res = self.client.get(f"{self.base}/products/{self.out_stock_prod['id']}")
        self.assertEqual(res.status_code, 200)
        html = res.content.decode()
        self.assertIn('data-disable-oos="false"', html)
        self.assertIn('"disable_out_of_stock": false', html)

    def test_product_card_out_of_stock_badge_in_collection(self):
        data = self.ok("get", "/api/v1/themes/settings/")
        col_template = data["templates"]["collection"]
        col_template["sections"]["main"]["settings"]["show_add_to_cart"] = True
        self.ok("patch", "/api/v1/themes/settings/", {
            "templates": {"collection": col_template}
        })
        res = self.client.get(f"{self.base}/collections/all")
        self.assertEqual(res.status_code, 200)
        html = res.content.decode()
        self.assertIn('button--disabled', html)
        self.assertIn('data-sold-out="true"', html)
        self.assertIn('Sold out', html)

    def test_backend_cart_api_rejects_out_of_stock_item(self):
        import re
        home = self.client.get(f"{self.base}/")
        match = re.search(r'data-cart-session="([0-9a-f]{32})"', home.content.decode())
        session_id = match.group(1) if match else "a" * 32
        res = self.client.post(
            f"{self.base}/cart/add",
            data={"product": self.out_stock_prod["id"], "quantity": 1},
            format="json",
            HTTP_X_CART_SESSION=session_id,
        )
        self.assertEqual(res.status_code, 400)
        data = res.json()
        self.assertIn("error", data)
        self.assertIn("sold out", data["error"].lower())


class ThemeSettingsPanelTest(StoreClientMixin, APITestCase):
    """Theme settings panel, phase 1: colour palette, typography and buttons."""

    def setUp(self):
        self.token, self.store = self.sign_up("panel@example.com", "Panel Store")

    def store_html(self):
        r = self.client.get(f"/s/{self.store['storeSlug']}/")
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def preview_html(self, settings):
        data = self.ok("get", "/api/v1/themes/settings/")
        data["settings"].update(settings)
        return self.ok("post", "/api/v1/themes/preview/?page=index", data)["html"]

    def test_defaults_render_as_before(self):
        from .rendering import theme_css
        from .schemas import resolve
        css = str(theme_css(resolve({})["settings"]))
        for line in ("--b-fw:600", "--b-bw:1px", "--b-mh:46px", "--b-px:26px", "--b-tt:none"):
            self.assertIn(line, css)
        self.assertTrue(css.endswith("}</style>") and css.count("{") == 1)  # :root only, no extra rules

    def test_schema_groups_and_hidden_phases(self):
        from .schemas import THEME_GROUPS, THEME_SETTINGS
        ids = [g["id"] for g in THEME_GROUPS]
        self.assertEqual(ids[:3], ["colors", "typography", "buttons"])
        self.assertNotIn("pagination", ids)  # SipNow pagination stays out of the shared settings
        self.assertNotIn("heading_weight", THEME_SETTINGS)  # sections set their own heading weight
        for key in ("heading_color", "link_color", "border_color", "heading_case", "base_font_size_mobile",
                    "button_border", "button2_bg", "button_focus_color", "link_underline"):
            self.assertIn(THEME_SETTINGS[key]["group"], ("colors", "typography", "buttons"))

    def test_save_applies_to_store_and_keeps_other_settings(self):
        self.ok("patch", "/api/v1/themes/settings/", {"settings": {"announcement": "Hello"}})
        self.ok("patch", "/api/v1/themes/settings/", {"settings": {
            "heading_color": "#112233", "link_color": "#AA0000", "border_color": "#CCCCCC",
            "heading_case": "uppercase", "heading_size": 40, "heading_size_mobile": 24,
            "base_font_size_mobile": 15, "body_line_height": "1.7",
            "button_border": "#000000", "button_border_width": 2, "button_size": "large", "button_weight": "800",
            "button_case": "uppercase", "button_hover_bg": "#333333", "button_focus_color": "#00FF00",
            "button_disabled_opacity": 40, "button2_bg": "#EEEEEE", "link_underline": "hover",
        }})
        saved = self.ok("get", "/api/v1/themes/settings/")["settings"]
        self.assertEqual(saved["announcement"], "Hello")  # unrelated setting kept
        self.assertEqual(saved["heading_case"], "uppercase")
        html = self.store_html()
        for part in ("--tss-heading-color:#112233", "--line:#CCCCCC", "--b-bd:#000000", "--b-bw:2px", "--b-mh:56px",
                     "--b-fw:800", "--b-tt:uppercase", "--b-hbg:#333333", "--b-focus:#00FF00", "--b-dis-op:0.4",
                     "--b2-bg:#EEEEEE", "h1,h2,h3{text-transform:uppercase}",
                     "@media (min-width:750px){body .section-heading{font-size:40px}}",
                     "@media (max-width:749px){body .section-heading{font-size:24px}}",
                     "body .button{border-color:var(--b-bd);border-width:var(--b-bw)}",
                     "--tss-base-size-m:15px", "body{line-height:1.7}",
                     "a:where(:not(.button)):hover{text-decoration:underline}"):
            self.assertIn(part, html)

    def test_preview_shows_unsaved_change_without_saving(self):
        html = self.preview_html({"heading_color": "#445566"})
        self.assertIn("--tss-heading-color:#445566", html)
        self.assertNotIn("--tss-heading-color", self.store_html())

    def test_invalid_values_are_refused(self):
        for bad in ({"heading_color": "red"}, {"heading_size": 500}, {"button_font": "comic"},
                    {"link_color": "#123;}</style>"}):
            self.ok("patch", "/api/v1/themes/settings/", {"settings": bad}, expect=400)
        # Unknown choices fall back to the default and numbers are clamped (as for every other setting)
        self.ok("patch", "/api/v1/themes/settings/", {"settings": {"heading_case": "950", "button_border_width": 9}})
        saved = self.ok("get", "/api/v1/themes/settings/")["settings"]
        self.assertEqual((saved["heading_case"], saved["button_border_width"]), ("none", 4))

    def test_unvalidated_values_cannot_break_out_of_style(self):
        from .rendering import theme_css
        from .schemas import resolve
        settings = resolve({})["settings"]
        settings.update({"heading_color": "red;}</style><script>x</script>", "heading_case": "x}</style>",
                         "heading_size": "40px}</style>"})
        css = str(theme_css(settings))
        self.assertNotIn("</style><", css)
        self.assertNotIn("script", css)

    def test_section_buttons_follow_theme_button_settings(self):
        from .rendering import _button_vars, _theme_button
        from .schemas import resolve
        settings = resolve({})["settings"]
        settings.update({"button_border": "#123456", "button_border_width": 3, "button_weight": "700"})
        theme = _theme_button(settings)
        vars_ = _button_vars({"b_bg": "#FF0000", "b_border_width": ""}, theme)
        self.assertEqual((vars_["--b-bd"], vars_["--b-bw"], vars_["--b-fw"]), ("#123456", "3px", "700"))
        # A section can still set any width, 1px included
        for width in ("0", "1", "4"):
            self.assertEqual(_button_vars({"b_border_width": width}, theme)["--b-bw"], f"{width}px")

    def test_saved_number_border_widths_still_load(self):
        from .schemas import resolve
        template = {"order": ["h"], "sections": {"h": {"type": "hero", "settings": {}}}}
        for saved, expected in ((1, ""), (2, "2"), (0, "0"), ("1", "1"), (9, "")):
            template["sections"]["h"]["settings"]["b_border_width"] = saved
            resolved = resolve({"templates": {"index": template}})
            self.assertEqual(resolved["templates"]["index"]["sections"]["h"]["settings"]["b_border_width"], expected)

    def test_mobile_body_size_is_not_overridden_by_type_css(self):
        from .rendering import TYPE_CSS
        rule = "@media (max-width:749px){body{font-size:var(--tss-base-size-m,var(--tss-base-size,16px))}}"
        self.assertIn(rule, str(TYPE_CSS))
        self.ok("patch", "/api/v1/themes/settings/", {"settings": {"base_font_size_mobile": 14}})
        html = self.store_html()
        self.assertIn("--tss-base-size-m:14px", html)
        self.assertLess(html.index("--tss-base-size-m:14px"), html.index(rule))


class ThemeSettingsPhase2Test(StoreClientMixin, APITestCase):
    """Theme settings panel, phase 2: palette fix, prices, product cards and the later groups."""

    def setUp(self):
        self.token, self.store = self.sign_up("phase2@example.com", "Phase Two Store")

    def store_html(self, path="/"):
        r = self.client.get(f"/s/{self.store['storeSlug']}{path}")
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def preview_html(self, settings):
        data = self.ok("get", "/api/v1/themes/settings/")
        data["settings"].update(settings)
        return self.ok("post", "/api/v1/themes/preview/?page=index", data)["html"]

    def save(self, settings, expect=200):
        return self.ok("patch", "/api/v1/themes/settings/", {"settings": settings}, expect=expect)

    def theme_style(self, html):
        start = html.index("<style>:root{--tss-heading-font")
        return html[start:html.index("</style>", start)]

    # Colour fix
    def test_palette_names_what_an_empty_colour_follows(self):
        from .schemas import THEME_SETTINGS
        from .section_sync import _field
        self.assertEqual(THEME_SETTINGS["button_bg"]["palette"], "Accent")
        self.assertEqual(THEME_SETTINGS["header_bg"]["palette"], "Background")
        self.assertEqual(_field({"type": "color", "id": "background", "default": ""})["palette"], "Background")
        self.assertNotIn("palette", _field({"type": "color", "id": "logo_color", "default": ""}))

    def test_button_colour_variable_follows_accent_until_the_button_has_its_own(self):
        css = self.theme_style(self.store_html())
        self.assertIn("--tss-button-color:var(--accent)", css)
        self.assertIn("--tss-button-text:var(--bg)", css)
        self.save({"button_bg": "#F5C400", "button_text": "#0F0F0F"})
        css = self.theme_style(self.store_html())
        self.assertIn("--tss-button-color:#F5C400", css)
        self.assertIn("--tss-button-text:#0F0F0F", css)
        self.save({"button_bg": ""})  # Use palette
        self.assertIn("--tss-button-color:var(--accent)", self.theme_style(self.store_html()))

    def test_bold_highlight_uses_the_button_colour_and_keeps_its_look(self):
        from .presets import build, get_preset
        files, custom_data, _ = build("bold")
        styles = files["snippets/theme-styles.liquid"]
        self.assertIn("--pop: var(--tss-button-color, #F5C400)", styles)
        self.assertIn("color: var(--tss-button-text, #0F0F0F)", styles)
        # Its saved colours are unchanged, so a new Bold store looks as before
        self.assertEqual(get_preset("bold")["settings"]["button_bg"], "#F5C400")
        self.assertEqual(custom_data["settings"]["button_bg"], "#F5C400")

    # Prices
    def test_untouched_store_has_no_price_rules(self):
        css = self.theme_style(self.store_html())
        self.assertNotIn(".price", css)

    def test_price_settings_reach_store_preview_and_reload(self):
        from .rendering import _COMPARE_PRICE, _PRICE, _SALE_PRICE
        values = {"price_color": "#112233", "price_sale_color": "#CC0000", "price_compare_color": "#999999",
                  "price_show_compare": False, "price_size": 26, "price_size_mobile": 17, "price_weight": "800"}
        expected = [f"{_PRICE},body .cart-line__price{{color:#112233;font-weight:800}}",
                    f"{_SALE_PRICE}{{color:#CC0000}}", f"{_COMPARE_PRICE}{{color:#999999}}",
                    f"{_COMPARE_PRICE}{{display:none}}",
                    f"@media (max-width:749px){{{_PRICE}{{font-size:17px}}}}",
                    f"@media (min-width:750px){{{_PRICE}{{font-size:26px}}}}"]
        preview = self.preview_html(values)
        for part in expected:
            self.assertIn(part, preview)
        self.assertNotIn("#CC0000", self.store_html())  # unsaved: preview only
        self.save(values)
        for html in (self.store_html(), self.store_html()):  # storefront, and after a reload
            for part in expected:
                self.assertIn(part, html)
        saved = self.ok("get", "/api/v1/themes/settings/")["settings"]
        self.assertEqual((saved["price_weight"], saved["price_show_compare"]), ("800", False))

    def test_price_values_are_validated(self):
        for bad in ({"price_color": "red"}, {"price_size": 99}, {"price_sale_color": "#12;}</style>"}):
            self.save(bad, expect=400)
        self.save({"price_weight": "950"})  # unknown choice: back to the theme's weight
        self.assertEqual(self.ok("get", "/api/v1/themes/settings/")["settings"]["price_weight"], "")

    def test_price_selectors_match_the_storefront_markup(self):
        # Cards and the product page use these classes, so the rules above reach them
        from pathlib import Path
        starter = Path(__file__).resolve().parent / "starter"
        card = (starter / "snippets/product-card.liquid").read_text(encoding="utf-8")
        product = (starter / "sections/main-product.liquid").read_text(encoding="utf-8")
        cart = (starter / "snippets/cart.liquid").read_text(encoding="utf-8")
        self.assertIn('class="product-card', card)
        self.assertIn('<span class="price price--compare">', card)
        self.assertIn('class="product__price" data-price', product)
        self.assertIn('"price price--compare product__compare"', product)  # the variant script keeps the class
        self.assertIn('class="cart-line__price"', cart)

    # Product cards
    def test_untouched_store_has_no_card_rules(self):
        css = self.theme_style(self.store_html())
        self.assertNotIn("--card-", css)
        self.assertNotIn(".product-card", css)

    def test_card_settings_reach_store_preview_and_reload(self):
        from .rendering import _CARD, _CARD_UNBORDERED, _THEME_RATIO
        values = {"card_bg": "#FFFFEE", "card_border_width": 2, "card_border_color": "#222222", "card_radius": 14,
                  "card_padding": 10, "card_shadow": "soft", "card_image_ratio": "portrait", "card_image_fit": "cover",
                  "card_image_padding": 0, "card_text_align": "center", "card_title_size": 18,
                  "card_title_size_mobile": 15, "card_title_weight": "700", "card_zoom": 110}
        expected = ["--card-bg:#FFFFEE", "--card-border:2px solid #222222", "--card-padding:10px",
                    "--card-media-padding:0px", "--card-media-fit:cover",
                    f"{_CARD}{{background:var(--card-bg)}}",
                    f"{_CARD_UNBORDERED}{{border:var(--card-border);padding:var(--card-padding)}}",
                    f"{_CARD}{{border-radius:14px;box-shadow:0 4px 12px rgba(0,0,0,.12);text-align:center}}",
                    f"{_CARD} :is(.price,.product-card__cta,.product-card__rating){{align-self:center}}",
                    f"body .product-card__media{_THEME_RATIO}{{aspect-ratio:3/4}}",
                    f"{_CARD} .product-card__title{{font-weight:700}}",
                    f"@media (max-width:749px){{{_CARD} .product-card__title{{font-size:15px}}}}",
                    f"@media (min-width:750px){{{_CARD} .product-card__title{{font-size:18px}}}}",
                    "body .product-card:hover .product-card__media img{transform:scale(1.1)}",
                    "@media (prefers-reduced-motion:reduce){body .product-card:hover .product-card__media img{transform:none}}"]
        preview = self.preview_html(values)
        for part in expected:
            self.assertIn(part, preview)
        self.save(values)
        for html in (self.store_html(), self.store_html(), self.store_html("/collections/all")):
            for part in expected:
                self.assertIn(part, html)

    def test_zoom_off_and_sections_without_zoom(self):
        self.save({"card_zoom": 100})
        css = self.theme_style(self.store_html())
        self.assertIn("body .product-card:hover .product-card__media img{transform:none}", css)
        self.save({"card_zoom": 112})
        css = self.theme_style(self.store_html())
        # A section with the Lift or None hover effect keeps it
        self.assertIn("body .card-hover--none .product-card:hover .product-card__media img,"
                      "body .card-hover--lift .product-card:hover .product-card__media img{transform:none}", css)

    def test_reduced_motion_turns_off_the_default_zoom(self):
        from .rendering import SECTION_CSS
        self.assertIn("@media (prefers-reduced-motion:reduce){.product-card .product-card__media img{transition:none}"
                      ".product-card:hover .product-card__media img{transform:none}}", str(SECTION_CSS))

    def test_section_card_settings_win_over_the_theme(self):
        self.save({"card_bg": "#FFFFEE", "card_image_ratio": "landscape"})
        data = self.ok("get", "/api/v1/themes/settings/")
        index = data["templates"]["index"]
        grid = next(i for i, sec in index["sections"].items() if sec["type"] in ("product-grid", "product-carousel"))
        self.assertEqual(index["sections"][grid]["settings"]["image_ratio"], "")  # new sections follow the theme
        index["sections"][grid]["settings"].update(card_background="#ABCDEF", image_ratio="square")
        self.ok("patch", "/api/v1/themes/settings/", {"templates": {"index": index}})
        html = self.store_html()
        self.assertIn("--card-bg: #ABCDEF;", html)  # on the section, closer to its cards than :root
        self.assertIn("product-card__media--square", html)  # excluded from the theme's shape rule

    def test_card_values_are_validated_and_cart_wishlist_badges_still_render(self):
        for bad in ({"card_bg": "blue"}, {"card_title_size": 99}):
            self.save(bad, expect=400)
        self.save({"card_radius": "9px;}"})  # a range never saves text
        self.assertIsInstance(self.ok("get", "/api/v1/themes/settings/")["settings"]["card_radius"], int)
        self.save({"card_shadow": "huge", "card_zoom": 400})  # unknown choice and clamped number
        saved = self.ok("get", "/api/v1/themes/settings/")["settings"]
        self.assertEqual((saved["card_shadow"], saved["card_zoom"]), ("none", 115))
        self.save({"card_padding": 4, "card_text_align": "right"})
        html = self.store_html()
        self.assertIn("--card-padding:4px", self.theme_style(html))
        self.assertIn("top:calc(var(--card-padding,16px) + 9px)", html)  # the wishlist heart follows the padding

    # Badges, logo and favicon, page, animations, buttons, cart, drawers, search, icons, inputs, menus
    LATER = {
        "badge_bg": "#333333", "badge_text": "#FFFFEE", "badge_new_bg": "#00AA00", "badge_corners": "square",
        "sale_badge_show": False, "sale_badge_bg": "#880000", "sale_badge_text": "#FFFFFF",
        "favicon_url": "https://cdn.example.com/icon.png", "logo_height_mobile": 28, "page_width": 1400,
        "animations_enabled": False, "animation_speed": "fast", "button_hover_border": "#445566", "button_press": "shrink",
        "cart_count_bg": "#123123", "cart_count_text": "#FAFAFA", "drawer_width": 520, "drawer_bg": "#F0F0F0",
        "drawer_overlay": 60, "search_submit_bg": "#202020", "search_submit_text": "#EEEEEE", "search_panel_width": 480,
        "icon_size": 24, "icon_stroke": "1.5", "input_corners": "pill", "input_border_color": "#999999",
        "input_bg": "#FFFFFF", "input_focus_color": "#0055FF", "input_height": 50, "popover_radius": 0,
        "popover_shadow": "none", "popover_border": False,
    }
    LATER_CSS = [
        "body .product-card__badge:not(.product-card__badge--new){background:#333333}",
        "body .product-card__badge{color:#FFFFEE}", "body .product-card__badge--new{background:#00AA00}",
        "body .product-card__badge{border-radius:0px}", "body .product-card__ribbon{display:none}",
        "body .product-card__ribbon{background:#880000;color:#FFFFFF}",
        "@media (max-width:749px){body .site-header__logo img{height:28px!important}}",
        "body .page-width{max-width:1400px}",
        "html.tss-js body :is(.tss-anim-fade,.tss-anim-slide-up){transition-duration:.35s}",
        "html.tss-js body :is(.tss-anim-fade,.tss-anim-slide-up){opacity:1;transform:none;transition:none}",
        "body .tss-marquee__track{animation:none}", "--b-hbd:#445566",
        'body .button:active:not(:disabled,[aria-disabled="true"]){transform:scale(.97)}',
        "body .site-header__cart-count{background:#123123;color:#FAFAFA}",
        "body .cart-drawer__panel{background:#F0F0F0;width:min(520px,100%)}",
        "body .cart-drawer__overlay{background:rgba(0,0,0,0.6)}",
        "body .site-header__search-submit{background:#202020;color:#EEEEEE}",
        "body .site-header__search-panel{width:min(92vw,480px)}",
        "svg{width:24px;height:24px;stroke-width:1.5}",
        "body .site-header__cart svg{width:28px;height:28px;stroke-width:1.3}",
        "body:not(.template-checkout) :is(.form-input,.newsletter__input):not(#tss){border-radius:999px;"
        "border-color:#999999;background:#FFFFFF;height:50px;min-height:0}",
        ":is(:focus,:focus-visible){border-color:#0055FF;outline-color:#0055FF}",
        "body .site-header__nav :is(.site-nav__dropdown,.mega-menu,.site-nav__more):not(#tss){border-radius:0px;box-shadow:none;"
        "border-color:transparent}",
    ]

    def test_untouched_store_has_no_rules_from_the_later_groups(self):
        html = self.store_html()
        css = self.theme_style(html)
        self.assertEqual(css.count("{"), 1, css[-300:])  # :root only
        self.assertNotIn('rel="icon"', html)

    def test_later_groups_reach_store_preview_and_reload(self):
        preview = self.preview_html(self.LATER)
        for part in self.LATER_CSS:
            self.assertIn(part, preview)
        self.save(self.LATER)
        for html in (self.store_html(), self.store_html(), self.store_html("/collections/all")):
            for part in self.LATER_CSS:
                self.assertIn(part, html)
            self.assertIn('<link rel="icon" href="https://cdn.example.com/icon.png">', html)
            self.assertLess(html.index('rel="icon"'), html.index("</head>"))
        saved = self.ok("get", "/api/v1/themes/settings/")["settings"]
        self.assertEqual({k: saved[k] for k in self.LATER}, self.LATER)

    def test_later_group_values_are_validated(self):
        for bad in ({"favicon_url": "javascript:alert(1)"}, {"badge_bg": "red"},
                    {"input_focus_color": "#000;}</style>"}):
            self.save(bad, expect=400)

    def test_theme_hover_border_is_a_default_sections_can_override(self):
        from .rendering import _button_vars, _theme_button
        from .schemas import resolve
        settings = resolve({})["settings"]
        settings["button_hover_border"] = "#445566"
        theme = _theme_button(settings)
        self.assertEqual(_button_vars({}, theme)["--b-hbd"], "#445566")
        self.assertEqual(_button_vars({"b_hover_border": "#000000"}, theme)["--b-hbd"], "#000000")
        self.assertEqual(_button_vars({}, _theme_button(resolve({})["settings"]))["--b-hbd"], "var(--accent)")

    # Custom CSS
    def test_store_custom_css_reaches_store_preview_and_reload_but_not_checkout(self):
        css = ".product-card__title{letter-spacing:.05em}@media (max-width:749px){.site-header{padding:4px}}"
        self.assertIn(f"<style data-tss-custom-css>{css}</style>", self.preview_html({"custom_css": css}))
        self.assertNotIn("data-tss-custom-css", self.store_html())  # unsaved
        self.save({"custom_css": css})
        for html in (self.store_html(), self.store_html(), self.store_html("/collections/all")):
            self.assertIn(f"<style data-tss-custom-css>{css}</style>", html)
        from .rendering import render_page
        from apps.tenancy.models import Store
        store = Store.objects.get(store_id=self.store["storeId"])
        self.assertNotIn("data-tss-custom-css", render_page(store, "checkout", editor=True))

    def test_unsafe_custom_css_is_refused(self):
        for bad in (
            "</style><script>alert(1)</script>", "a{color:red}</style>", "@import url(https://x.test/a.css);",
            "a{background:url(http://x.test/a.png)}", "a{background:url(javascript:alert(1))}",
            "a{background:url('data:image/svg+xml,x')}", "a{width:expression(alert(1))}", "a{color:red}}b{color:red",
            'a{content:"{"}}body{display:none}', "a{color:red", "a{color:\\72 ed}", "a{behavior:url(https://x.test/x.htc)}",
            "x" * 1501,
        ):
            self.save({"custom_css": bad}, expect=400)
        self.save({"custom_css": "a{background:url(https://cdn.example.com/a.png)}"})

    def test_section_custom_css_is_scoped_to_its_section(self):
        data = self.ok("get", "/api/v1/themes/settings/")
        index = data["templates"]["index"]
        sid = index["order"][0]
        index["sections"][sid]["settings"]["custom_css"] = "padding:2px;.section-heading{color:#C00000}"
        self.ok("patch", "/api/v1/themes/settings/", {"templates": {"index": index}})
        html = self.store_html()
        self.assertIn(f'<style>[data-tss-css="{sid}"]{{padding:2px;.section-heading{{color:#C00000}}}}</style>', html)
        self.assertRegex(html, rf'<div class="tss-wrap[^>]*data-tss-css="{sid}"')  # on the section's wrapper
        self.assertEqual(html.count("data-tss-css="), 2)  # only that section
        index["sections"][sid]["settings"]["custom_css"] = "x" * 501
        self.ok("patch", "/api/v1/themes/settings/", {"templates": {"index": index}}, expect=400)
        index["sections"][sid]["settings"]["custom_css"] = "}body{display:none}{"
        self.ok("patch", "/api/v1/themes/settings/", {"templates": {"index": index}}, expect=400)

    def test_other_stores_do_not_get_the_css(self):
        self.save({"custom_css": ".site-header{outline:1px solid red}"})
        self.client.credentials()
        _token, other = self.sign_up("other-css@example.com", "Other CSS Store")
        r = self.client.get(f"/s/{other['storeSlug']}/")
        self.assertNotIn("data-tss-custom-css", r.content.decode())
