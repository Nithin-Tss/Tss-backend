"""
Section ("design block") schemas for the starter theme.

Each section type has settings with defaults, optional repeatable blocks
(e.g. slides), and the page templates it may be placed on. A store's own
choices live in StoreTheme.custom_data:

    {
      "settings": {"accent_color": "#161C2C", ...},       # theme-wide
      "templates": {
        "index": {
          "order": ["hero", "featured"],
          "sections": {
            "hero": {"type": "hero", "settings": {...}, "blocks": [...]},
            ...
          }
        },
        "product": {...},
        "collection": {...}
      }
    }

Anything missing falls back to the defaults below.
"""
import copy
import re
import uuid
from html import escape as html_escape
from html.parser import HTMLParser

from rest_framework import serializers

PAGES = ("index", "product", "collection", "blog", "article", "page")
# Alternate page templates (Shopify's page.contact): templates/page.<name>.liquid, edited like the Page template
PAGE_TEMPLATE = re.compile(r"^page\.[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_PAGE_TEMPLATES = 100


def is_page_key(key):
    """A page the customizer can edit: a built-in one, or an alternate page template (page.<name>)."""
    return key in PAGES or (isinstance(key, str) and len(key) <= 40 and bool(PAGE_TEMPLATE.match(key)))


def base_page(key):
    """"page.contact" -> "page"; sections are allowed per base page."""
    return key.split(".", 1)[0]
# Pages where product lists make sense (the blog and content pages are for reading)
COMMERCE_PAGES = ("index", "product", "collection")

THEME_SETTINGS = {
    "accent_color": {"type": "color", "label": "Accent color", "default": "#161C2C"},
    "background_color": {"type": "color", "label": "Background color", "default": "#FFFFFF"},
    "text_color": {"type": "color", "label": "Text color", "default": "#161C2C"},
    "currency_symbol": {"type": "text", "label": "Currency symbol", "default": "₹", "max": 4},
    "announcement": {"type": "text", "label": "Announcement text", "default": ""},
}

_HEIGHT = {"type": "select", "label": "Height", "options": ["small", "medium", "large"], "default": "medium"}
_ALIGN = {"type": "select", "label": "Text alignment", "options": ["left", "center", "right"], "default": "center"}
_COLLECTION = {
    "type": "collection",
    "label": "Collection name or id (empty = newest products)",
    "default": "",
}

SECTIONS = {
    "hero": {
        "name": "Hero banner",
        "pages": PAGES,
        "settings": {
            "image_url": {"type": "image", "label": "Background image URL", "default": ""},
            "heading": {"type": "text", "label": "Heading", "default": "Welcome to our store"},
            "subheading": {"type": "textarea", "label": "Subheading", "default": "Discover products made for you."},
            "button_label": {"type": "text", "label": "Button label", "default": "Shop now"},
            "button_link": {"type": "url", "label": "Button link", "default": "/collections/all"},
            "text_alignment": _ALIGN,
            "overlay_opacity": {"type": "range", "label": "Image overlay (%)", "min": 0, "max": 90, "default": 30},
            "height": _HEIGHT,
        },
    },
    "slideshow": {
        "name": "Slideshow",
        "pages": PAGES,
        "settings": {
            "autoplay": {"type": "checkbox", "label": "Auto-rotate slides", "default": True},
            "interval": {"type": "range", "label": "Seconds per slide", "min": 3, "max": 15, "default": 5},
            "height": _HEIGHT,
        },
        "blocks": {
            "slide": {
                "name": "Slide",
                "settings": {
                    "image_url": {"type": "image", "label": "Image URL", "default": ""},
                    "heading": {"type": "text", "label": "Heading", "default": "New season"},
                    "text": {"type": "textarea", "label": "Text", "default": ""},
                    "button_label": {"type": "text", "label": "Button label", "default": ""},
                    "button_link": {"type": "url", "label": "Button link", "default": ""},
                    "text_alignment": _ALIGN,
                },
            },
        },
        "max_blocks": 8,
    },
    "product-carousel": {
        "name": "Product carousel",
        "pages": COMMERCE_PAGES,
        "uses_products": True,
        "settings": {
            "heading": {"type": "text", "label": "Heading", "default": "Trending now"},
            "collection": _COLLECTION,
            "limit": {"type": "range", "label": "Products to show", "min": 2, "max": 24, "default": 10},
            "autoplay": {"type": "checkbox", "label": "Auto-scroll", "default": False},
            "show_vendor": {"type": "checkbox", "label": "Show vendor", "default": False},
        },
    },
    "product-grid": {
        "name": "Featured products",
        "pages": COMMERCE_PAGES,
        "uses_products": True,
        "settings": {
            "heading": {"type": "text", "label": "Heading", "default": "Featured products"},
            "collection": _COLLECTION,
            "limit": {"type": "range", "label": "Products to show", "min": 1, "max": 24, "default": 8},
            "columns": {"type": "range", "label": "Columns on desktop", "min": 2, "max": 5, "default": 4},
            "show_vendor": {"type": "checkbox", "label": "Show vendor", "default": False},
        },
    },
    "rich-text": {
        "name": "Rich text",
        "pages": PAGES,
        "settings": {
            "heading": {"type": "text", "label": "Heading", "default": "About us"},
            "text": {"type": "textarea", "label": "Text", "default": "Tell customers about your brand."},
            "text_alignment": _ALIGN,
        },
    },
    "main-product": {
        "name": "Product information",
        "pages": ("product",),
        "required": True,
        "settings": {
            "show_vendor": {"type": "checkbox", "label": "Show vendor", "default": True},
            "show_sku": {"type": "checkbox", "label": "Show SKU", "default": False},
            "show_quantity": {"type": "checkbox", "label": "Show quantity selector", "default": True},
            "image_ratio": {
                "type": "select",
                "label": "Image shape",
                "options": ["square", "portrait", "landscape"],
                "default": "square",
            },
        },
    },
    "main-collection": {
        "name": "Collection products",
        "pages": ("collection",),
        "required": True,
        "settings": {
            "columns": {"type": "range", "label": "Columns on desktop", "min": 2, "max": 5, "default": 4},
            "show_vendor": {"type": "checkbox", "label": "Show vendor", "default": False},
        },
    },
}

# Home page "All products" grid: every Active product on the Online Store, newest
# first, 12 at a time with "Load more", and "View all" to the all-products page
ALL_PRODUCTS_SETTINGS = {
    "heading": "All products",
    "source": "newest",
    "limit": 12,
    "load_more": True,
    "view_all_label": "View all",
    "view_all_link": "/collections/all",
}

DEFAULT_TEMPLATES = {
    "index": {
        "order": ["slideshow", "featured", "carousel", "hero", "about"],
        "sections": {
            "slideshow": {
                "type": "slideshow",
                "settings": {},
                "blocks": [
                    {
                        "type": "slide",
                        "settings": {
                            "heading": "New arrivals",
                            "text": "Fresh picks for this season.",
                            "button_label": "Shop new",
                            "button_link": "/collections/all",
                        },
                    },
                    {
                        "type": "slide",
                        "settings": {
                            "heading": "Best sellers",
                            "text": "The products everyone loves.",
                            "button_label": "Explore",
                            "button_link": "/collections/all",
                        },
                    },
                    {
                        "type": "slide",
                        "settings": {"heading": "Free shipping", "text": "On all orders this week."},
                    },
                ],
            },
            "featured": {"type": "product-grid", "settings": ALL_PRODUCTS_SETTINGS},
            "carousel": {"type": "product-carousel", "settings": {"heading": "Trending now"}},
            "hero": {
                "type": "hero",
                "settings": {
                    "heading": "Made with care",
                    "subheading": "Quality you can feel, prices you will love.",
                    "height": "small",
                },
            },
            "about": {"type": "rich-text", "settings": {}},
        },
    },
    "product": {
        "order": ["main", "related"],
        "sections": {
            "main": {"type": "main-product", "settings": {}},
            "related": {"type": "product-carousel", "settings": {"heading": "You may also like", "limit": 8}},
        },
    },
    "collection": {
        "order": ["main"],
        "sections": {
            "main": {"type": "main-collection", "settings": {}},
        },
    },
}

MAX_SECTIONS_PER_PAGE = 25
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")

# Extra setting types (products, collections, richtext, embed): {type: clean(spec, value, where)}
VALUE_CLEANERS = {}


def _clean_value(spec, value, where):
    kind = spec["type"]

    if kind in VALUE_CLEANERS:
        return VALUE_CLEANERS[kind](spec, value, where)

    if kind == "checkbox":
        if not isinstance(value, bool):
            raise serializers.ValidationError(f"{where} must be true or false.")
        return value

    if kind == "range":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise serializers.ValidationError(f"{where} must be a number.")
        if not spec["min"] <= value <= spec["max"]:
            raise serializers.ValidationError(f"{where} must be between {spec['min']} and {spec['max']}.")
        return int(value)

    if not isinstance(value, str):
        raise serializers.ValidationError(f"{where} must be text.")

    value = value.strip()

    if kind == "select" and value not in spec["options"]:
        raise serializers.ValidationError(f"{where} must be one of: {', '.join(spec['options'])}.")
    if kind == "color" and not _COLOR.match(value):
        raise serializers.ValidationError(f"{where} must be a color like #1A2B3C.")
    if kind == "image" and value and not value.startswith(("/", "https://", "http://")):
        raise serializers.ValidationError(f"{where} must start with /, http:// or https://.")
    # Links may also be an email, a phone number or a section on the page
    if kind == "url" and value and not value.startswith(("/", "https://", "http://", "mailto:", "tel:", "#")):
        raise serializers.ValidationError(f"{where} must be a page (/...), a web address, mailto:, tel: or #section.")
    if len(value) > spec.get("max", 2000):
        raise serializers.ValidationError(f"{where} is too long.")

    return value


def _clean_settings(schema, raw, where):
    if not isinstance(raw, dict):
        raise serializers.ValidationError(f"{where} must be an object.")

    unknown = set(raw) - set(schema)
    if unknown:
        raise serializers.ValidationError(f"{where} has unknown settings: {', '.join(sorted(unknown))}.")

    return {key: _clean_value(schema[key], value, f"{where}.{key}") for key, value in raw.items()}


def _clean_section(page, section_id, raw):
    where = f"templates.{page}.sections.{section_id}"

    if not isinstance(raw, dict) or raw.get("type") not in SECTIONS:
        raise serializers.ValidationError(f"{where}.type must be one of: {', '.join(SECTIONS)}.")

    schema = SECTIONS[raw["type"]]

    if base_page(page) not in schema["pages"]:
        raise serializers.ValidationError(f"{where}: {schema['name']} can't be used on the {page} page.")

    # Hidden sections stay saved but aren't shown on the store
    hidden = raw.get("hidden", False)
    if not isinstance(hidden, bool):
        raise serializers.ValidationError(f"{where}.hidden must be true or false.")

    raw_settings = raw.get("settings", {})
    legacy_heart = None
    if isinstance(raw_settings, dict) and "show_wishlist" in raw_settings and "wishlist_heart" in schema.get("blocks", {}) \
            and raw["type"] != "main-product":
        # The old "Show wishlist heart" checkbox: true is a Wishlist heart block, false takes it away
        raw_settings = {k: v for k, v in raw_settings.items() if k != "show_wishlist"}
        legacy_heart = raw["settings"]["show_wishlist"]

    section = {
        "type": raw["type"],
        "hidden": hidden,
        "settings": _clean_settings(schema["settings"], raw_settings, f"{where}.settings"),
    }

    if "blocks" in schema:
        blocks = raw.get("blocks", [])
        if not isinstance(blocks, list) or len(blocks) > schema["max_blocks"]:
            raise serializers.ValidationError(f"{where}.blocks must be a list of at most {schema['max_blocks']}.")

        section["blocks"] = []
        for i, block in enumerate(blocks):
            if not isinstance(block, dict) or block.get("type") not in schema["blocks"]:
                raise serializers.ValidationError(f"{where}.blocks[{i}].type is not valid.")
            block_hidden = block.get("hidden", False)
            if not isinstance(block_hidden, bool):
                raise serializers.ValidationError(f"{where}.blocks[{i}].hidden must be true or false.")
            block_schema = schema["blocks"][block["type"]]
            section["blocks"].append({
                "type": block["type"],
                "hidden": block_hidden,
                "settings": _clean_settings(
                    block_schema["settings"], block.get("settings", {}), f"{where}.blocks[{i}].settings"
                ),
            })

        if legacy_heart is True and not any(b["type"] == "wishlist_heart" for b in section["blocks"]):
            section["blocks"].append({"type": "wishlist_heart", "hidden": False, "settings": {}})
        elif legacy_heart is False:
            section["blocks"] = [b for b in section["blocks"] if b["type"] != "wishlist_heart"]

        # Some block types have their own limit (e.g. at most 8 slides)
        for type_, limit in schema.get("type_limits", {}).items():
            if sum(1 for b in section["blocks"] if b["type"] == type_) > limit:
                name = schema["blocks"][type_]["name"].lower()
                raise serializers.ValidationError(f"{where} can have at most {limit} {name}s.")

    return section


def validate_custom_data(data):
    """Validate a full custom_data document from the API. Returns a clean copy."""
    if not isinstance(data, dict):
        raise serializers.ValidationError("Expected an object.")

    clean = {
        "settings": _clean_settings(THEME_SETTINGS, data.get("settings", {}), "settings"),
        "templates": {},
    }
    templates = data.get("templates", {})

    if not isinstance(templates, dict) or not all(is_page_key(key) for key in templates):
        raise serializers.ValidationError(f"templates may only contain: {', '.join(PAGES)}, and page.<name> templates.")
    if sum(1 for key in templates if key not in PAGES) > MAX_PAGE_TEMPLATES:
        raise serializers.ValidationError(f"A theme can have at most {MAX_PAGE_TEMPLATES} page templates.")

    for page, raw in templates.items():
        if not isinstance(raw, dict):
            raise serializers.ValidationError(f"templates.{page} must be an object.")

        order = raw.get("order", [])
        sections = raw.get("sections", {})

        if not isinstance(order, list) or not isinstance(sections, dict):
            raise serializers.ValidationError(f"templates.{page} needs an order list and a sections object.")
        if len(sections) > MAX_SECTIONS_PER_PAGE:
            raise serializers.ValidationError(f"templates.{page} has too many sections.")
        if sorted(order) != sorted(sections):
            raise serializers.ValidationError(f"templates.{page}.order must list every section exactly once.")

        cleaned = {}
        for section_id, section in sections.items():
            if not _ID.match(section_id):
                raise serializers.ValidationError(f"templates.{page}: section id '{section_id}' is not valid.")
            cleaned[section_id] = _clean_section(page, section_id, section)

        for type_, schema in SECTIONS.items():
            if schema.get("required") and base_page(page) in schema["pages"] and not any(
                s["type"] == type_ and not s["hidden"] for s in cleaned.values()
            ):
                raise serializers.ValidationError(f"templates.{page} must include a {schema['name']} section.")

        clean["templates"][page] = {"order": order, "sections": cleaned}

    # Synced sections (the footer) keep their values here; they are checked against the
    # section's own {% schema %} when saved (services.save_section_values), not here
    if isinstance(data.get("sections"), dict):
        clean["sections"] = data["sections"]

    return clean


def _coerce(spec, value):
    """A saved value that no longer fits its option (range or choices changed) falls back safely."""
    if spec["type"] in ("range", "number"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return copy.deepcopy(spec["default"])
        return max(spec["min"], min(spec["max"], int(value)))
    if spec["type"] == "select" and value not in spec["options"]:
        return copy.deepcopy(spec["default"])
    return value


def _with_defaults(schema, values):
    return {
        key: _coerce(spec, values[key]) if key in values else copy.deepcopy(spec["default"])
        for key, spec in schema.items()
    }


def default_template(page):
    """The sections a new template of `page` starts with (page.contact -> the Page template's)."""
    return copy.deepcopy(DEFAULT_TEMPLATES[base_page(page)])


def resolve(custom_data):
    """custom_data merged over the defaults: what the renderer uses."""
    custom_data = custom_data or {}
    templates = copy.deepcopy(DEFAULT_TEMPLATES)
    templates.update(copy.deepcopy(custom_data.get("templates", {})))

    for page in templates.values():
        for section in page["sections"].values():
            schema = SECTIONS[section["type"]]
            section["hidden"] = bool(section.get("hidden", False))
            heart_saved = (section.get("settings") or {}).get("show_wishlist") is True
            section["settings"] = _with_defaults(schema["settings"], section.get("settings", {}))
            if "blocks" in schema:
                section["blocks"] = [
                    {
                        "type": b["type"],
                        "hidden": bool(b.get("hidden", False)),
                        "settings": _with_defaults(schema["blocks"][b["type"]]["settings"], b.get("settings", {})),
                    }
                    for b in section.get("blocks", [])
                    if b.get("type") in schema["blocks"]  # block types that no longer exist are dropped
                ]
                # A card section saved with the old "Show wishlist heart" on has the heart as a block now
                if heart_saved and section["type"] != "main-product" and "wishlist_heart" in schema["blocks"] \
                        and not any(b["type"] == "wishlist_heart" for b in section["blocks"]):
                    section["blocks"].append({"type": "wishlist_heart", "hidden": False,
                                              "settings": _with_defaults(schema["blocks"]["wishlist_heart"]["settings"], {})})
                # Blocks the section always has, e.g. "Product information" on layouts saved before it existed
                for type_ in reversed(schema.get("required_blocks", [])):
                    if not any(b["type"] == type_ for b in section["blocks"]):
                        section["blocks"].insert(0, {
                            "type": type_,
                            "hidden": False,
                            "settings": _with_defaults(schema["blocks"][type_]["settings"], {}),
                        })

    return {
        "settings": _with_defaults(THEME_SETTINGS, custom_data.get("settings", {})),
        "templates": templates,
        # Synced sections' values (see section_sync), passed through as saved
        **({"sections": copy.deepcopy(custom_data["sections"])} if isinstance(custom_data.get("sections"), dict) else {}),
    }


def public_schema():
    """Schemas for the admin customizer UI."""
    return {
        "settings": THEME_SETTINGS,
        "sections": SECTIONS,
        "pages": list(PAGES),
        "groups": GROUPS,
        "theme_groups": THEME_GROUPS,
        "fonts": [{"key": k, "label": label, "stack": stack, "google": family} for k, label, stack, family, _ in FONTS],
    }


# ---------------------------------------------------------------------------
# Page builder: HTML cleaning for content owners type into the customizer.
# sanitize_html: rich text. sanitize_embed: custom HTML / embed blocks.
# Scripts, styles, event handlers, unknown tags and attributes are removed.
# ---------------------------------------------------------------------------

_RICH_TAGS = {"p", "br", "strong", "b", "em", "i", "u", "ul", "ol", "li", "a", "h3", "h4", "blockquote"}
_VOID_TAGS = {"br"}
_EMBED_TAGS = _RICH_TAGS | {
    "div", "span", "img", "iframe", "h1", "h2", "h5", "h6", "hr", "figure", "figcaption",
    "small", "sup", "sub", "code", "pre", "table", "thead", "tbody", "tr", "td", "th",
}
_EMBED_VOID = _VOID_TAGS | {"img", "hr"}
_SAFE_LINK = re.compile(r"^(https?://|mailto:|/(?!/))", re.IGNORECASE)
_SAFE_SRC = re.compile(r"^(https://|http://|/(?!/))", re.IGNORECASE)
# Embeds may only frame these sites (music, maps, forms, booking)
_IFRAME_HOSTS = re.compile(
    r"^https://(www\.)?(youtube\.com|youtube-nocookie\.com|player\.vimeo\.com|open\.spotify\.com|"
    r"w\.soundcloud\.com|google\.com/maps|maps\.google\.com|docs\.google\.com/forms|calendly\.com)(/|$)",
    re.IGNORECASE,
)
_SIZE = re.compile(r"^\d{1,4}%?$")


class _RichTextCleaner(HTMLParser):
    tags = _RICH_TAGS
    void = _VOID_TAGS

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.open = []
        self.skip = 0  # inside <script>/<style>: drop the contents too

    def attributes(self, tag, attrs):
        """Allowed attributes for `tag`, already escaped. None drops the tag."""
        return ""

    def _tag(self, tag):
        # Browser editors wrap new lines in <div>; in rich text that's a paragraph
        return "p" if tag == "div" and "div" not in self.tags else tag

    def handle_starttag(self, tag, attrs):
        tag = self._tag(tag)
        if tag in ("script", "style"):
            self.skip += 1
            return
        if self.skip or tag not in self.tags:
            return
        if tag == "a":
            href = (dict(attrs).get("href") or "").strip()
            if _SAFE_LINK.match(href):
                self.out.append(f'<a href="{html_escape(href, quote=True)}" rel="noopener noreferrer">')
            else:
                self.out.append("<a>")
        else:
            extra = self.attributes(tag, dict(attrs))
            if extra is None:
                return
            self.out.append(f"<{tag}{extra}>")
        if tag not in self.void:
            self.open.append(tag)

    def handle_endtag(self, tag):
        tag = self._tag(tag)
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag not in self.open:
            return
        # Close anything still open inside this tag, then the tag itself
        while self.open:
            current = self.open.pop()
            self.out.append(f"</{current}>")
            if current == tag:
                break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(html_escape(data, quote=False))

    def result(self):
        return "".join(self.out) + "".join(f"</{t}>" for t in reversed(self.open))


class _EmbedCleaner(_RichTextCleaner):
    tags = _EMBED_TAGS
    void = _EMBED_VOID

    def attributes(self, tag, attrs):
        def attr(name, value):
            return f' {name}="{html_escape(value, quote=True)}"'

        size = "".join(
            attr(name, attrs[name]) for name in ("width", "height") if _SIZE.match(str(attrs.get(name) or ""))
        )

        if tag == "img":
            src = (attrs.get("src") or "").strip()
            if not _SAFE_SRC.match(src):
                return None
            return attr("src", src) + attr("alt", attrs.get("alt") or "") + size + ' loading="lazy"'

        if tag == "iframe":
            src = (attrs.get("src") or "").strip()
            if not _IFRAME_HOSTS.match(src):
                return None
            title = attr("title", attrs.get("title") or "Embedded content")
            return attr("src", src) + title + size + ' loading="lazy" referrerpolicy="strict-origin-when-cross-origin"'

        if tag in ("td", "th"):
            return "".join(
                attr(name, attrs[name]) for name in ("colspan", "rowspan") if str(attrs.get(name) or "").isdigit()
            )

        return ""


def _run_cleaner(cleaner, value):
    cleaner.feed(str(value or ""))
    cleaner.close()
    return cleaner.result()


def sanitize_html(value):
    """Keep simple formatting (bold, italic, lists, links); strip everything else."""
    return _run_cleaner(_RichTextCleaner(), value)


def sanitize_embed(value):
    """Layout tags, images and trusted iframes only; scripts and event handlers removed."""
    return _run_cleaner(_EmbedCleaner(), value)


# ---------------------------------------------------------------------------
# Page builder: extra setting types
# ---------------------------------------------------------------------------


def _clean_id_list(spec, value, where):
    limit = spec.get("max", 24)
    if not isinstance(value, list) or len(value) > limit:
        raise serializers.ValidationError(f"{where} must be a list of at most {limit} items.")
    ids = []
    for item in value:
        try:
            ids.append(str(uuid.UUID(str(item))))
        except ValueError:
            raise serializers.ValidationError(f"{where} contains an invalid id.")
    return list(dict.fromkeys(ids))


def _html_cleaner(cleaner):
    def clean(spec, value, where):
        if not isinstance(value, str):
            raise serializers.ValidationError(f"{where} must be text.")
        if len(value) > spec.get("max", 20000):
            raise serializers.ValidationError(f"{where} is too long.")
        return cleaner(value).strip()

    return clean


VALUE_CLEANERS.update({
    "products": _clean_id_list,
    "collections": _clean_id_list,
    "richtext": _html_cleaner(sanitize_html),
    "embed": _html_cleaner(sanitize_embed),
})


# A section the owner names and fills with any blocks, in any order
SECTIONS["custom"] = {
    "name": "Custom section",
    "pages": PAGES,
    "settings": {
        "heading": {"type": "text", "label": "Section name", "default": "New section", "max": 200},
        "show_heading": {"type": "checkbox", "label": "Show the name as a heading", "default": True},
        "text_alignment": dict(_ALIGN, default="left"),
    },
    "blocks": {
        "heading": {
            "name": "Heading",
            "settings": {
                "text": {"type": "text", "label": "Text", "default": "Heading", "max": 300},
                "size": {"type": "select", "label": "Size", "options": ["small", "medium", "large"], "default": "medium"},
            },
        },
        "text": {
            "name": "Text",
            "settings": {
                "body": {"type": "richtext", "label": "Text", "default": "<p>Write something here.</p>", "max": 20000},
            },
        },
        "image": {
            "name": "Image",
            "settings": {
                "image_url": {"type": "image", "label": "Image", "default": ""},
                "alt": {"type": "text", "label": "Description (for screen readers)", "default": "", "max": 300},
                "link": {"type": "url", "label": "Link (optional)", "default": ""},
            },
        },
        "button": {
            "name": "Button",
            "settings": {
                "label": {"type": "text", "label": "Button text", "default": "Shop now", "max": 100},
                "link": {"type": "url", "label": "Button link", "default": "/collections/all"},
                "style": {"type": "select", "label": "Style", "options": ["filled", "outline"], "default": "filled"},
            },
        },
        "products": {
            "name": "Product list",
            "settings": {
                "products": {"type": "products", "label": "Products", "default": [], "max": 24},
                "columns": {"type": "range", "label": "Columns on desktop", "min": 2, "max": 5, "default": 4},
            },
        },
        "collections": {
            "name": "Collection list",
            "settings": {
                "collections": {"type": "collections", "label": "Collections", "default": [], "max": 12},
            },
        },
        "html": {
            "name": "Custom HTML / embed",
            "settings": {
                "html": {"type": "embed", "label": "HTML or embed code", "default": "", "max": 20000},
            },
        },
    },
    "max_blocks": 30,
}


# ---------------------------------------------------------------------------
# Page builder: section options. Every option has a group for the customizer
# ("content", "layout", "style", "advanced"); "ui", "unit", "help", "labels"
# and "show_if" only affect how the customizer shows it.
# ---------------------------------------------------------------------------

_ANCHOR = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_CSS_CLASSES = re.compile(r"^[A-Za-z0-9_\- ]*$")


def _clean_optional_color(spec, value, where):
    if value in ("", None):
        return ""
    if not isinstance(value, str) or not _COLOR.match(value.strip()):
        raise serializers.ValidationError(f"{where} must be a color like #1A2B3C, or empty.")
    return value.strip().upper()


def _clean_anchor(spec, value, where):
    if not isinstance(value, str):
        raise serializers.ValidationError(f"{where} must be text.")
    value = value.strip().lstrip("#").lower()
    if value and (len(value) > spec.get("max", 50) or not _ANCHOR.match(value)):
        raise serializers.ValidationError(f"{where} may only use letters, numbers, - and _.")
    return value


def _clean_css_class(spec, value, where):
    if not isinstance(value, str):
        raise serializers.ValidationError(f"{where} must be text.")
    value = " ".join(value.split())
    if len(value) > spec.get("max", 100) or not _CSS_CLASSES.match(value):
        raise serializers.ValidationError(f"{where} may only use letters, numbers, -, _ and spaces.")
    return value


def _clean_number(spec, value, where):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise serializers.ValidationError(f"{where} must be a number.")
    if not spec["min"] <= value <= spec["max"]:
        raise serializers.ValidationError(f"{where} must be between {spec['min']} and {spec['max']}.")
    return int(value)


VALUE_CLEANERS.update({
    "color_optional": _clean_optional_color,
    "anchor": _clean_anchor,
    "css_class": _clean_css_class,
    "number": _clean_number,
})


def _opt(group, type_, label, default, **extra):
    return {"type": type_, "label": label, "default": default, "group": group, **extra}


def _choice(group, label, options, default, ui=None, **extra):
    spec = _opt(group, "select", label, default, options=list(options), **extra)
    if ui:
        spec["ui"] = ui
    return spec


def common_settings(width="wide", spacing_top=48, spacing_bottom=48, alignment="default"):
    """Layout, style and advanced options every section has."""
    return {
        "width": _choice("layout", "Width", ["full", "wide", "narrow"], width, ui="segmented"),
        "spacing_top": _opt("layout", "range", "Top spacing", spacing_top, min=0, max=120, step=4, unit="px"),
        "spacing_bottom": _opt("layout", "range", "Bottom spacing", spacing_bottom, min=0, max=120, step=4, unit="px"),
        "padding_x": _opt(
            "layout", "range", "Left and right padding", 20, min=0, max=80, step=4, unit="px",
            help="Space between the content and the edges of the screen.",
        ),
        "content_alignment": _choice(
            "layout", "Content alignment", ["default", "left", "center", "right"], alignment, ui="segmented",
        ),
        "background_color": _opt("style", "color_optional", "Background colour", ""),
        "text_color": _opt("style", "color_optional", "Text colour", ""),
        "heading_size": _choice("style", "Heading size", ["small", "medium", "large"], "medium", ui="segmented"),
        "heading_weight": _choice(
            "style", "Heading weight", ["regular", "medium", "semibold", "bold"], "bold",
        ),
        "border_radius": _opt(
            "style", "range", "Corner radius", 0, min=0, max=40, step=2, unit="px",
            help="Rounds the corners of the section's background.",
        ),
        "divider": _choice(
            "style", "Divider line", ["none", "top", "bottom", "both"], "none",
            labels={"both": "Top and bottom"},
        ),
        "visibility": _choice(
            "advanced", "Show on", ["both", "desktop", "mobile"], "both", ui="segmented",
            labels={"both": "All devices", "desktop": "Desktop only", "mobile": "Mobile only"},
        ),
        "animation": _choice(
            "advanced", "Entrance animation", ["none", "fade", "slide-up"], "none",
            labels={"fade": "Fade in", "slide-up": "Slide up"},
        ),
        "anchor_id": _opt(
            "advanced", "anchor", "Section ID", "", max=50,
            help="Lets a menu link jump here: link to #your-id. Letters, numbers, - and _.",
        ),
        "css_class": _opt(
            "advanced", "css_class", "Custom CSS class", "", max=100,
            help="Adds your own class names, for styles written in the code editor.",
        ),
    }


_ALIGN3 = ["left", "center", "right"]

SECTIONS["hero"]["settings"] = {
    "label_text": _opt("content", "text", "Label above the heading", "", max=80),
    "heading": _opt("content", "text", "Heading", "Welcome to our store"),
    "subheading": _opt("content", "textarea", "Sub-text", "Discover products made for you."),
    "button_label": _opt("content", "text", "Button text", "Shop now", help="Leave empty to hide the button."),
    "button_link": _opt("content", "url", "Button link", "/collections/all"),
    "button_new_tab": _opt("content", "checkbox", "Open the button link in a new tab", False),
    "button2_label": _opt("content", "text", "Second button text", "", help="Optional."),
    "button2_link": _opt("content", "url", "Second button link", ""),
    "button2_new_tab": _opt("content", "checkbox", "Open the second button link in a new tab", False),
    "image_url": _opt("content", "image", "Background image", ""),
    "image_alt": _opt("content", "text", "Image description", "", max=200, help="Optional. Read aloud by screen readers."),
    "mobile_image_url": _opt(
        "content", "image", "Mobile image", "", help="Optional. Shown on phones instead of the background image.",
    ),
    "video_url": _opt(
        "content", "url", "Background video", "",
        help="Optional. A direct link to an .mp4 or .webm file; it plays muted behind the text.",
    ),
    "height": _choice(
        "layout", "Height", ["small", "medium", "large", "full", "custom"], "medium",
        labels={"full": "Full screen"},
    ),
    "custom_height": _opt(
        "layout", "number", "Custom height", 560, min=200, max=1400, unit="px", show_if={"height": "custom"},
    ),
    "vertical_position": _choice("layout", "Text position", ["top", "middle", "bottom"], "middle", ui="segmented"),
    "text_alignment": _choice("layout", "Text alignment", _ALIGN3, "center", ui="segmented"),
    "text_max_width": _opt("layout", "range", "Maximum text width", 720, min=320, max=1100, step=20, unit="px"),
    "overlay_color": _opt("style", "color", "Image overlay colour", "#000000"),
    "overlay_opacity": _opt("style", "range", "Image overlay opacity", 30, min=0, max=90, unit="%"),
    "button_style": _choice("style", "Button style", ["filled", "outline"], "filled", ui="segmented"),
    "button_color": _opt("style", "color_optional", "Button colour", "", help="None uses the theme's colour."),
    "image_position": _choice("style", "Image position", ["center", "top", "bottom"], "center", ui="segmented"),
    "parallax": _opt(
        "style", "checkbox", "Parallax effect", False, help="The image stays still while the page scrolls.",
    ),
    **common_settings(width="full", spacing_top=0, spacing_bottom=0),
}

SECTIONS["slideshow"]["settings"] = {
    "heading": _opt("content", "text", "Section heading", "", help="Optional. Shown above the slideshow."),
    "height": _choice("layout", "Height", ["small", "medium", "large", "full"], "medium", labels={"full": "Full screen"}),
    "show_arrows": _opt("layout", "checkbox", "Show arrows", True),
    "show_dots": _opt("layout", "checkbox", "Show dots", True),
    "autoplay": _opt("style", "checkbox", "Autoplay", True),
    "interval": _opt("style", "range", "Speed", 5, min=3, max=10, unit="s", help="Seconds each slide is shown."),
    "transition": _choice("style", "Transition", ["slide", "fade"], "slide", ui="segmented"),
    "overlay_opacity": _opt("style", "range", "Image overlay opacity", 30, min=0, max=90, unit="%"),
    **common_settings(width="full", spacing_top=0, spacing_bottom=0),
}
SECTIONS["slideshow"]["blocks"]["slide"]["settings"] = {
    "media_type": _choice("content", "Type", ["image", "video"], "image", ui="segmented", labels={"image": "Image", "video": "Video"}),
    "image_url": _opt("content", "image", "Image", ""),
    "image_alt": dict(_opt("content", "text", "Image description", "", max=200, help="Optional. Read aloud by screen readers."), show_if={"media_type": "image"}),
    "mobile_image_url": _opt(
        "content", "image", "Mobile image", "", help="Optional. Shown on phones.", show_if={"media_type": "image"},
    ),
    "video_url": _opt(
        "content", "url", "Video link", "", show_if={"media_type": "video"},
        help="Link to an MP4 or WEBM file. The image above is shown until it loads.",
    ),
    "heading": _opt("content", "text", "Heading", "New season"),
    "text": _opt("content", "textarea", "Sub-text", ""),
    "button_label": _opt("content", "text", "Button text", ""),
    "button_link": _opt("content", "url", "Button link", ""),
    "text_alignment": _choice("content", "Text alignment", _ALIGN3, "center", ui="segmented"),
    "image_fit": _choice(
        "layout", "Image fit", ["cover", "contain"], "cover", ui="segmented", show_if={"media_type": "image"},
        labels={"cover": "Fill (crop)", "contain": "Fit (whole image)"},
    ),
    "image_focus": _choice(
        "layout", "Image focus", ["center", "top", "bottom", "left", "right"], "center",
        show_if={"media_type": "image"}, help="Which part stays visible when the image is cropped.",
    ),
    "content_direction": _choice(
        "layout", "Direction", ["vertical", "horizontal"], "vertical", ui="segmented",
        labels={"vertical": "Vertical", "horizontal": "Horizontal"},
    ),
    "content_align": _choice(
        "layout", "Alignment", ["", "start", "center", "end"], "",
        labels={"": "Same as text alignment", "start": "Start", "center": "Center", "end": "End"},
    ),
    "content_position": _choice(
        "layout", "Position", ["top", "center", "bottom"], "center",
        labels={"top": "Top", "center": "Center", "bottom": "Bottom"},
    ),
    "content_gap": _opt("layout", "range", "Gap", 16, min=0, max=100, unit="px"),
    "background_color": _opt("style", "color_optional", "Background colour", ""),
    "overlay": _opt("style", "checkbox", "Media overlay", True),
    "overlay_color": _opt("style", "color", "Overlay colour", "#000000", show_if={"overlay": True}),
    "overlay_style": _choice(
        "style", "Overlay style", ["solid", "gradient"], "solid", ui="segmented", show_if={"overlay": True},
        labels={"solid": "Solid", "gradient": "Gradient"},
    ),
    "gradient_direction": _choice(
        "style", "Gradient direction", ["up", "down"], "up", ui="segmented",
        show_if={"overlay": True, "overlay_style": "gradient"}, labels={"up": "Up", "down": "Down"},
    ),
    "padding_top": _opt("layout", "range", "Top padding", 48, min=0, max=200, unit="px"),
    "padding_bottom": _opt("layout", "range", "Bottom padding", 48, min=0, max=200, unit="px"),
    "padding_left": _opt("layout", "range", "Left padding", 20, min=0, max=200, unit="px"),
    "padding_right": _opt("layout", "range", "Right padding", 20, min=0, max=200, unit="px"),
}


def _product_list_settings(layout, limit, heading):
    return {
        "heading": _opt("content", "text", "Section heading", heading),
        "subheading": _opt("content", "textarea", "Sub-text", ""),
        "source": _choice(
            "content", "Products to show", ["collection", "products", "newest", "best_sellers"], "collection",
            labels={"collection": "From a collection", "products": "Choose products", "best_sellers": "Best sellers"},
        ),
        "collection": dict(_COLLECTION, group="content", label="Collection", show_if={"source": "collection"}),
        "products": _opt("content", "products", "Products", [], max=12, show_if={"source": "products"}),
        "limit": _opt("content", "range", "Number of products", limit, min=2, max=12),
        "load_more": _opt(
            "content", "checkbox", "Show \"Load more\" button", False,
            help="Shows the number of products above first, then more on each click.",
        ),
        "view_all_label": _opt("content", "text", "\"View all\" button text", "", help="Leave empty to hide it."),
        "view_all_link": _opt("content", "url", "\"View all\" link", "/collections/all"),
        "layout": _choice("layout", "Display as", ["grid", "carousel"], layout, ui="segmented"),
        "columns": _opt("layout", "range", "Products per row (desktop)", 4, min=2, max=6),
        "columns_mobile": _opt("layout", "range", "Products per row (mobile)", 2, min=1, max=3),
        "gap": _opt("layout", "range", "Gap between cards", 24, min=0, max=48, step=2, unit="px"),
        "image_ratio": _choice("layout", "Image shape", ["square", "portrait", "landscape"], "square", ui="segmented"),
        "show_arrows": _opt("layout", "checkbox", "Show arrows", True, show_if={"layout": "carousel"}),
        "autoplay": _opt("layout", "checkbox", "Auto-scroll", False, show_if={"layout": "carousel"}),
        "show_price": _opt("style", "checkbox", "Show price", True),
        "show_vendor": _opt("style", "checkbox", "Show vendor", False),
        "show_rating": _opt("style", "checkbox", "Show rating", False, help="From approved product reviews."),
        "show_tags": _opt("style", "checkbox", "Show product tags", False),
        "show_add_to_cart": _opt(
            "style", "checkbox", "Show \"Add to cart\" button", False,
            help="Opens the product page, where shoppers pick options and add it to the cart.",
        ),
        "card_background": _opt("style", "color_optional", "Card background", ""),
        "card_border": _opt("style", "checkbox", "Card border", False),
        "card_hover": _choice(
            "style", "Card hover effect", ["none", "lift", "zoom"], "zoom", ui="segmented",
            labels={"zoom": "Zoom image"},
        ),
        **common_settings(),
    }


SECTIONS["product-carousel"]["settings"] = _product_list_settings("carousel", 10, "Trending now")
SECTIONS["product-grid"]["settings"] = _product_list_settings("grid", 8, "Featured products")

SECTIONS["rich-text"]["settings"] = {
    "heading": _opt("content", "text", "Section heading", "About us"),
    "text": _opt("content", "richtext", "Text", "<p>Tell customers about your brand.</p>", max=20000),
    "button_label": _opt("content", "text", "Button text", "", help="Optional."),
    "button_link": _opt("content", "url", "Button link", "/collections/all"),
    "image_url": _opt("content", "image", "Image", "", help="Optional."),
    "image_alt": dict(_opt("content", "text", "Image description", "", max=200, help="Optional. Read aloud by screen readers."), show_if={"image_url": "*"}),
    "image_position": _choice(
        "content", "Image position", ["left", "right", "top"], "left", ui="segmented", show_if={"image_url": "*"},
    ),
    "image_ratio": _choice(
        "layout", "Image shape", ["original", "square", "portrait", "landscape", "wide"], "original",
        labels={"original": "Original", "wide": "Wide (21:9)"}, show_if={"image_url": "*"},
        help="Original keeps the picture's own proportions.",
    ),
    "image_fit": _choice(
        "layout", "Image fit", ["cover", "contain"], "cover", ui="segmented", show_if={"image_url": "*"},
        labels={"cover": "Fill (crop)", "contain": "Fit (whole image)"},
    ),
    "image_focus": _choice(
        "layout", "Image focus", ["center", "top", "bottom", "left", "right"], "center", show_if={"image_url": "*"},
        help="Which part stays visible when the image is cropped.",
    ),
    "text_width": _choice("layout", "Text width", ["narrow", "medium", "wide"], "medium", ui="segmented"),
    "text_alignment": _choice("layout", "Text alignment", _ALIGN3, "center", ui="segmented"),
    "text_columns": _choice("layout", "Columns", ["1", "2"], "1", ui="segmented"),
    "text_size": _choice("style", "Text size", ["small", "medium", "large"], "medium", ui="segmented"),
    "line_spacing": _choice("style", "Line spacing", ["tight", "normal", "relaxed"], "normal", ui="segmented"),
    "quote_style": _choice(
        "style", "Quote style", ["plain", "bordered", "highlighted"], "bordered", ui="segmented",
        help="How quotes in the text look.",
    ),
    **common_settings(),
}

SECTIONS["custom"]["settings"] = {
    "heading": _opt("content", "text", "Section name", "New section", max=200),
    "show_heading": _opt("content", "checkbox", "Show the name as a heading", True),
    **common_settings(alignment="left"),
}

# "Load more" only works on the grid, so the carousel doesn't offer it
SECTIONS["product-carousel"]["settings"].pop("load_more", None)

SECTIONS["main-collection"]["settings"] = {
    "show_image": _opt("content", "checkbox", "Show the collection image", True),
    "show_description": _opt("content", "checkbox", "Show the collection description", True),
    "show_count": _opt("content", "checkbox", "Show the number of products", True),
    "show_sort": _opt("content", "checkbox", "Show the \"Sort by\" menu", True),
    "empty_text": _opt("content", "text", "Text when there are no products", "No products yet.", max=200),
    "columns": _opt("layout", "range", "Products per row (desktop)", 4, min=2, max=6),
    "columns_mobile": _opt("layout", "range", "Products per row (mobile)", 2, min=1, max=3),
    "gap": _opt("layout", "range", "Gap between cards", 24, min=0, max=48, step=2, unit="px"),
    "image_ratio": _choice("layout", "Image shape", ["square", "portrait", "landscape"], "square", ui="segmented"),
    "show_price": _opt("style", "checkbox", "Show price", True),
    "show_vendor": _opt("style", "checkbox", "Show vendor", False),
    "show_rating": _opt("style", "checkbox", "Show rating", False, help="From approved product reviews."),
    "show_tags": _opt("style", "checkbox", "Show product tags", False),
    "show_add_to_cart": _opt(
        "style", "checkbox", "Show \"Add to cart\" button", False,
        help="Opens the product page, where shoppers pick options and add it to the cart.",
    ),
    "card_background": _opt("style", "color_optional", "Card background", ""),
    "card_border": _opt("style", "checkbox", "Card border", False),
    "card_hover": _choice(
        "style", "Card hover effect", ["none", "lift", "zoom"], "zoom", ui="segmented",
        labels={"zoom": "Zoom image"},
    ),
    **common_settings(),
}

SECTIONS["main-product"]["settings"] = {
    "show_vendor": _opt("content", "checkbox", "Show vendor", True),
    "show_price": _opt("content", "checkbox", "Show price", True),
    "show_stock": _opt("content", "checkbox", "Show stock message", True),
    "low_stock_threshold": _opt(
        "content", "range", "\"Only a few left\" at or below", 5, min=1, max=20, show_if={"show_stock": True},
        help="Shows the low-stock message when this many or fewer are in stock.",
    ),
    "show_sku": _opt("content", "checkbox", "Show SKU", False),
    "show_quantity": _opt("content", "checkbox", "Show quantity selector", True),
    "show_description": _opt("content", "checkbox", "Show description", True),
    "add_to_cart_label": _opt("content", "text", "Add to cart button text", "Add to cart", max=40),
    "sold_out_label": _opt("content", "text", "Sold out button text", "Sold out", max=40),
    "show_thumbnails": _opt("layout", "checkbox", "Show image thumbnails", True),
    "media_position": _choice("layout", "Image position", ["left", "right"], "left", ui="segmented"),
    "image_ratio": _choice("layout", "Image shape", ["square", "portrait", "landscape"], "square", ui="segmented"),
    "image_fit": _choice(
        "layout", "Image fit", ["cover", "contain"], "cover", ui="segmented",
        labels={"cover": "Fill (crop)", "contain": "Fit (whole image)"},
    ),
    **common_settings(),
}

# ---------------------------------------------------------------------------
# Blog: the post list (blog page), one post (article page) and a "Latest
# posts" section for any page. Until posts can be written in the admin, the
# customizer previews these pages with sample posts (see rendering.py).
# ---------------------------------------------------------------------------

THEME_SETTINGS.update({
    "blog_title": {"type": "text", "label": "Blog title", "default": "Blog", "max": 120, "group": "blog"},
    "blog_description": {
        "type": "textarea", "label": "Blog description", "default": "", "max": 500, "group": "blog",
        "help": "Shown under the title on the blog page.",
    },
})


def _post_card_settings():
    """What a post card shows; shared by the blog page and "Latest posts"."""
    return {
        "image_ratio": _choice("layout", "Image shape", ["landscape", "wide", "square", "portrait"], "landscape",
                               ui="segmented"),
        "show_image": _opt("style", "checkbox", "Show cover image", True),
        "show_excerpt": _opt("style", "checkbox", "Show excerpt", True),
        "show_date": _opt("style", "checkbox", "Show date", True),
        "show_author": _opt("style", "checkbox", "Show author", True),
        "show_category": _opt("style", "checkbox", "Show category", True),
        "show_reading_time": _opt("style", "checkbox", "Show reading time", True),
        "card_style": _choice("style", "Card style", ["plain", "bordered", "shadow"], "plain", ui="segmented"),
    }


SECTIONS["main-blog"] = {
    "name": "Blog posts",
    "pages": ("blog",),
    "required": True,
    "settings": {
        "show_header": _opt("content", "checkbox", "Show blog title and description", True),
        "show_filters": _opt("content", "checkbox", "Show category links", True),
        "posts_per_page": _opt("content", "range", "Posts per page", 9, min=3, max=24),
        "layout": _choice("layout", "Layout", ["grid", "list", "featured"], "grid", ui="segmented",
                          labels={"featured": "First post large"}),
        "columns": _opt("layout", "range", "Posts per row (desktop)", 3, min=1, max=4, show_if={"layout": "grid"}),
        **_post_card_settings(),
        **common_settings(),
    },
}

SECTIONS["main-article"] = {
    "name": "Article",
    "pages": ("article",),
    "required": True,
    "settings": {
        "show_cover": _opt("content", "checkbox", "Show cover image", True),
        "show_author": _opt("content", "checkbox", "Show author", True),
        "show_date": _opt("content", "checkbox", "Show date", True),
        "show_reading_time": _opt("content", "checkbox", "Show reading time", True),
        "show_tags": _opt("content", "checkbox", "Show tags", True),
        "show_share": _opt("content", "checkbox", "Show share links", True),
        "show_author_bio": _opt("content", "checkbox", "Show author bio under the post", True),
        "show_prev_next": _opt("content", "checkbox", "Show previous and next post", True),
        "related_count": _opt("content", "range", "Related posts", 3, min=0, max=6, help="0 hides them."),
        "content_width": _choice("layout", "Text width", ["narrow", "medium", "wide"], "medium", ui="segmented"),
        "cover_style": _choice("layout", "Cover image", ["contained", "full"], "contained", ui="segmented",
                               labels={"full": "Full width"}),
        **common_settings(),
    },
}

# Pages (About us, Shipping policy, ...): written in the admin (Pages); /pages/<handle> shows one.
SECTIONS["main-page"] = {
    "name": "Page content",
    "pages": ("page",),
    "required": True,
    "settings": {
        "show_title": _opt("content", "checkbox", "Show page title", True),
        "show_updated": _opt("content", "checkbox", "Show \"last updated\" date", False),
        "title_align": _choice("layout", "Title alignment", ["left", "center"], "left", ui="segmented"),
        "content_width": _choice("layout", "Text width", ["narrow", "medium", "wide"], "medium", ui="segmented"),
        **common_settings(),
    },
}

SECTIONS["featured-posts"] = {
    "name": "Latest posts",
    "pages": PAGES,
    "uses_articles": True,
    "settings": {
        "heading": _opt("content", "text", "Section heading", "From the blog"),
        "subheading": _opt("content", "textarea", "Sub-text", ""),
        "category": _opt("content", "text", "Category", "", max=120,
                         help="Optional. Only posts from this category; empty shows all."),
        "limit": _opt("content", "range", "Number of posts", 3, min=1, max=12),
        "view_all_label": _opt("content", "text", "\"View all\" link text", "View all posts",
                               help="Leave empty to hide it."),
        "layout": _choice("layout", "Display as", ["grid", "list"], "grid", ui="segmented"),
        "columns": _opt("layout", "range", "Posts per row (desktop)", 3, min=1, max=4, show_if={"layout": "grid"}),
        **_post_card_settings(),
        **common_settings(),
    },
}

DEFAULT_TEMPLATES["blog"] = {"order": ["main"], "sections": {"main": {"type": "main-blog", "settings": {}}}}
DEFAULT_TEMPLATES["article"] = {"order": ["main"], "sections": {"main": {"type": "main-article", "settings": {}}}}
DEFAULT_TEMPLATES["page"] = {"order": ["main"], "sections": {"main": {"type": "main-page", "settings": {}}}}

# Page templates every store starts with besides the base Page template. Each has a file
# (starter/templates/page.<name>.liquid) and these sections; a store can edit or delete them.
STARTER_PAGE_TEMPLATES = {
    "page.about": {
        "order": ["intro", "main"],
        "sections": {
            "intro": {"type": "rich-text", "settings": {
                "heading": "Our story",
                "text": "Tell customers who you are, how the store started and what you care about.",
            }},
            "main": {"type": "main-page", "settings": {}},
        },
    },
    "page.contact": {
        "order": ["main"],
        "sections": {"main": {"type": "main-page", "settings": {"title_align": "center", "content_width": "narrow"}}},
    },
    "page.faq": {
        "order": ["main", "questions"],
        "sections": {
            "main": {"type": "main-page", "settings": {"title_align": "center", "content_width": "narrow"}},
            "questions": {"type": "custom", "settings": {"heading": "Frequently asked questions"}, "blocks": [
                {"type": "accordion", "settings": {"open_first": True}},
            ]},
        },
    },
    "page.policy": {
        "order": ["main"],
        "sections": {"main": {"type": "main-page", "settings": {"content_width": "narrow", "show_updated": True}}},
    },
}

GROUPS = [
    {"id": "content", "label": "Content"},
    {"id": "layout", "label": "Layout"},
    {"id": "style", "label": "Style"},
    {"id": "advanced", "label": "Advanced"},
]


# ---------------------------------------------------------------------------
# Page builder: blocks. Every section can hold these blocks (after its own
# content); a custom section is made only of them. Each block also has its
# own spacing, alignment and width.
# ---------------------------------------------------------------------------

_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")


def _clean_datetime(spec, value, where):
    if not isinstance(value, str):
        raise serializers.ValidationError(f"{where} must be text.")
    value = value.strip()[:16]
    if value and not _DATETIME.match(value):
        raise serializers.ValidationError(f"{where} must be a date and time like 2026-12-31T23:59.")
    return value


VALUE_CLEANERS["datetime"] = _clean_datetime

ICONS = ["star", "heart", "truck", "shield", "gift", "leaf", "clock", "phone", "mail", "check", "return", "sparkle"]


def block_settings():
    """Spacing, alignment and width every block has."""
    return {
        "spacing_top": _opt("layout", "range", "Top spacing", 0, min=0, max=80, step=4, unit="px"),
        "spacing_bottom": _opt("layout", "range", "Bottom spacing", 0, min=0, max=80, step=4, unit="px"),
        "alignment": _choice("layout", "Alignment", ["default", "left", "center", "right"], "default", ui="segmented"),
        "width": _choice(
            "layout", "Width", ["auto", "full", "narrow"], "auto", ui="segmented",
            help="Full spans every column of a multi-column section; narrow keeps the block slim.",
        ),
    }


def _block(name, **settings):
    return {"name": name, "settings": {**settings, **block_settings()}}


BLOCKS = {
    "heading": _block(
        "Heading",
        text=_opt("content", "text", "Text", "Heading", max=300),
        size=_choice("content", "Size", ["small", "medium", "large"], "medium", ui="segmented"),
    ),
    "text": _block(
        "Text",
        body=_opt("content", "richtext", "Text", "<p>Write something here.</p>", max=20000),
    ),
    "image": _block(
        "Image",
        image_url=_opt("content", "image", "Image", ""),
        alt=_opt("content", "text", "Description (for screen readers)", "", max=300),
        link=_opt("content", "url", "Link (optional)", ""),
    ),
    "button": _block(
        "Button",
        label=_opt("content", "text", "Button text", "Shop now", max=100),
        link=_opt("content", "url", "Button link", "/collections/all"),
        new_tab=_opt("content", "checkbox", "Open in a new tab", False),
        style=_choice("content", "Style", ["filled", "outline"], "filled", ui="segmented"),
    ),
    "video": _block(
        "Video",
        url=_opt(
            "content", "url", "Video file link", "",
            help="A direct link to an .mp4 or .webm file. YouTube and Vimeo links can't play here.",
        ),
        poster=_opt("content", "image", "Cover image", "", help="Optional. Shown before the video plays."),
        autoplay=_opt("content", "checkbox", "Play automatically (muted, looping)", False),
        controls=_opt("content", "checkbox", "Show player controls", True),
    ),
    "products": _block(
        "Product list",
        products=_opt("content", "products", "Products", [], max=24),
        columns=_opt("content", "range", "Columns on desktop", 4, min=2, max=5),
    ),
    "collections": _block(
        "Collection list",
        collections=_opt("content", "collections", "Collections", [], max=12),
    ),
    "divider": _block(
        "Divider",
        line_style=_choice("content", "Line style", ["solid", "dashed", "dotted"], "solid", ui="segmented"),
        thickness=_opt("content", "range", "Thickness", 1, min=1, max=6, unit="px"),
        color=_opt("content", "color_optional", "Colour", "", help="None uses a light line."),
    ),
    "spacer": _block(
        "Spacer",
        height=_opt("content", "range", "Height", 40, min=8, max=200, step=4, unit="px"),
    ),
    "icon_text": _block(
        "Icon with text",
        icon=_choice("content", "Icon", ICONS, "star"),
        title=_opt("content", "text", "Title", "Free shipping", max=120),
        text=_opt("content", "textarea", "Text", "On all orders over ₹999."),
        icon_position=_choice("content", "Icon position", ["top", "left"], "top", ui="segmented"),
    ),
    "list": _block(
        "List",
        items=_opt("content", "textarea", "Items", "First item\nSecond item\nThird item", help="One item per line."),
        list_style=_choice("content", "Style", ["bullets", "numbers", "checks"], "bullets", ui="segmented"),
    ),
    "quote": _block(
        "Quote",
        text=_opt("content", "textarea", "Quote", "The best purchase I've made this year."),
        author=_opt("content", "text", "Author", "", max=120),
        quote_style=_choice("content", "Style", ["plain", "bordered", "highlighted"], "bordered", ui="segmented"),
    ),
    "social": _block(
        "Social links",
        instagram=_opt("content", "url", "Instagram", ""),
        facebook=_opt("content", "url", "Facebook", ""),
        x=_opt("content", "url", "X (Twitter)", ""),
        tiktok=_opt("content", "url", "TikTok", ""),
        youtube=_opt("content", "url", "YouTube", ""),
        pinterest=_opt("content", "url", "Pinterest", ""),
        linkedin=_opt("content", "url", "LinkedIn", ""),
    ),
    "countdown": _block(
        "Countdown timer",
        title=_opt("content", "text", "Title", "Sale ends in", max=120),
        end=_opt("content", "datetime", "Ends at", "", help="Uses the shopper's own time zone."),
        expired_text=_opt("content", "text", "Text after it ends", "This offer has ended.", max=200),
    ),
    "accordion": _block(
        "Accordion (FAQ)",
        items=_opt(
            "content", "textarea", "Questions and answers",
            "How long does delivery take?\nUsually 3 to 5 working days.\n\nCan I return an item?\nYes, within 30 days.",
            max=10000,
            help="Question on the first line, answer below it. Leave an empty line between questions.",
        ),
        open_first=_opt("content", "checkbox", "Open the first question", False),
    ),
    "map": _block(
        "Map or embed",
        address=_opt("content", "text", "Address", "", max=300, help="Shows this address on Google Maps."),
        embed=_opt(
            "content", "embed", "Or embed code", "", max=20000,
            help="Optional. Used instead of the address, e.g. a Google Maps or Calendly embed.",
        ),
        height=_opt("content", "range", "Height", 360, min=160, max=800, step=10, unit="px"),
    ),
    "html": _block(
        "Custom HTML",
        html=_opt("content", "embed", "HTML", "", max=20000),
    ),
}

GENERIC_BLOCK_TYPES = set(BLOCKS)

for _type, _schema in SECTIONS.items():
    own = {k: v for k, v in _schema.get("blocks", {}).items() if k not in GENERIC_BLOCK_TYPES}
    _schema["blocks"] = {**own, **BLOCKS}
    _schema["max_blocks"] = 40

SECTIONS["slideshow"]["type_limits"] = {"slide": 8}

# The product's Additional details (allergens, storage, ...) as accordion rows
# under the description. Every product page has exactly one: it can be hidden,
# not deleted (resolve() adds it back to older saved layouts).
SECTIONS["main-product"]["blocks"] = {
    "product_info": {
        "name": "Product information",
        "settings": {
            "heading": _opt("content", "text", "Heading", "Product information", max=120, help="Leave empty for no heading."),
            "open_first": _opt("content", "checkbox", "Open the first row by default", False),
            "allow_multiple": _opt(
                "content", "checkbox", "Allow several rows open at once", False,
                help="Off: opening a row closes the one that was open.",
            ),
            "font_size": _opt("style", "range", "Font size", 16, min=12, max=24, unit="px"),
            "divider_color": _opt("style", "color_optional", "Divider color", "", help="None uses the theme's light line."),
        },
    },
    # The heart that saves the product to the shopper's wishlist (see snippets/wishlist.liquid)
    "wishlist_heart": {
        "name": "Wishlist heart",
        "settings": {
            "label": _opt("content", "text", "Label", "Add to wishlist", max=40, help="Leave empty to show only the heart."),
            "size": _opt("style", "range", "Icon size", 20, min=16, max=40, step=2, unit="px"),
            "align": _choice("style", "Position", ["left", "center", "right"], "left", ui="segmented"),
        },
    },
    # A cart icon with the number of items, for the product page (see snippets/cart.liquid). The cart's drawer and
    # checkout options are the header's "Cart icon" block's.
    "cart_icon": {
        "name": "Cart icon",
        "settings": {
            "label": _opt("content", "text", "Label (for screen readers)", "Cart", max=40),
            "show_count": _opt("content", "checkbox", "Show the number of items", True),
            "opens": _choice(
                "content", "Clicking the icon opens", ["page", "drawer"], "page", ui="segmented",
                labels={"page": "Cart page", "drawer": "Side drawer"},
            ),
            "size": _opt("style", "range", "Icon size", 24, min=18, max=40, step=2, unit="px"),
            "align": _choice("style", "Position", ["left", "center", "right"], "left", ui="segmented"),
        },
    },
    **SECTIONS["main-product"]["blocks"],
}
SECTIONS["main-product"]["type_limits"] = {"product_info": 1, "wishlist_heart": 1, "cart_icon": 1}

# The wishlist heart on product cards (snippets/wishlist.liquid) is a block the same way: the owner picks "Wishlist
# heart" in "Add block", hides it with the eye or deletes it, and the cards always show the heart while the block
# is there and visible. It works on its own: nothing in it depends on the cart icon block. (The old "Show wishlist
# heart" checkbox is gone: a saved `show_wishlist: true` is read as a Wishlist heart block, see resolve().)
# The cart icon on product cards (see snippets/product-card.liquid and snippets/cart.liquid). Nothing adds it: the
# owner picks "Cart icon" in "Add block" (one per section), hides it with the block's eye or deletes it, and the
# cards show the icon on hover while the block is there and visible.
for _type in ("product-grid", "product-carousel", "main-collection"):
    SECTIONS[_type]["blocks"] = {
        "cart_icon": {
            "name": "Cart icon",
            "settings": {
                "label": _opt("content", "text", "Label (for screen readers)", "Add to cart", max=40),
                "size": _opt("style", "range", "Icon size", 44, min=32, max=64, step=2, unit="px"),
            },
        },
        **SECTIONS[_type]["blocks"],
    }
    SECTIONS[_type]["blocks"] = {
        "wishlist_heart": {
            "name": "Wishlist heart",
            "settings": {
                "label": _opt("content", "text", "Label (tooltip)", "Add to wishlist", max=40),
                "size": _opt("style", "range", "Icon size", 20, min=16, max=40, step=2, unit="px"),
            },
        },
        **SECTIONS[_type]["blocks"],
    }
    SECTIONS[_type]["type_limits"] = {**SECTIONS[_type].get("type_limits", {}), "cart_icon": 1, "wishlist_heart": 1}
SECTIONS["main-product"]["required_blocks"] = ["product_info"]

SECTIONS["custom"]["settings"] = {
    "heading": SECTIONS["custom"]["settings"]["heading"],
    "show_heading": SECTIONS["custom"]["settings"]["show_heading"],
    "columns": _opt("layout", "range", "Columns", 1, min=1, max=4, help="On phones, blocks always stack."),
    "block_gap": _opt("layout", "range", "Gap between blocks", 20, min=0, max=64, step=2, unit="px"),
    "vertical_align": _choice(
        "layout", "Vertical alignment", ["top", "center", "bottom", "stretch"], "top", ui="segmented",
        help="How blocks line up in a row when they have different heights.",
    ),
    **{k: v for k, v in SECTIONS["custom"]["settings"].items() if k not in ("heading", "show_heading")},
}


# ---------------------------------------------------------------------------
# Page builder: fonts, text options and button options. Options marked
# "theme_default" start empty/at their default and then follow the theme
# settings; the customizer shows "Reset to theme default" once one is changed.
# ---------------------------------------------------------------------------

# key, label, CSS font stack, Google Fonts family (None = no download), weights to load
FONTS = [
    ("system", "System default", 'system-ui,-apple-system,"Segoe UI",Roboto,sans-serif', None, ""),
    ("serif", "Serif (default)", 'Georgia,"Times New Roman",serif', None, ""),
    ("mono", "Monospace (default)", 'ui-monospace,SFMono-Regular,Menlo,Consolas,monospace', None, ""),
    ("arial", "Arial", "Arial,Helvetica,sans-serif", None, ""),
    ("georgia", "Georgia", "Georgia,serif", None, ""),
    ("verdana", "Verdana", "Verdana,Geneva,sans-serif", None, ""),
    ("trebuchet", "Trebuchet MS", '"Trebuchet MS",Helvetica,sans-serif', None, ""),
    ("times", "Times New Roman", '"Times New Roman",Times,serif', None, ""),
    ("inter", "Inter", '"Inter",system-ui,sans-serif', "Inter", "300;400;500;600;700;800"),
    ("poppins", "Poppins", '"Poppins",system-ui,sans-serif', "Poppins", "300;400;500;600;700;800"),
    ("roboto", "Roboto", '"Roboto",system-ui,sans-serif', "Roboto", "300;400;500;700"),
    ("open-sans", "Open Sans", '"Open Sans",system-ui,sans-serif', "Open Sans", "300;400;500;600;700;800"),
    ("montserrat", "Montserrat", '"Montserrat",system-ui,sans-serif', "Montserrat", "300;400;500;600;700;800"),
    ("lato", "Lato", '"Lato",system-ui,sans-serif', "Lato", "300;400;700"),
    ("dm-sans", "DM Sans", '"DM Sans",system-ui,sans-serif', "DM Sans", "300;400;500;600;700;800"),
    ("raleway", "Raleway", '"Raleway",system-ui,sans-serif', "Raleway", "300;400;500;600;700;800"),
    ("nunito", "Nunito", '"Nunito",system-ui,sans-serif', "Nunito", "300;400;500;600;700;800"),
    ("work-sans", "Work Sans", '"Work Sans",system-ui,sans-serif', "Work Sans", "300;400;500;600;700;800"),
    ("manrope", "Manrope", '"Manrope",system-ui,sans-serif', "Manrope", "300;400;500;600;700;800"),
    ("rubik", "Rubik", '"Rubik",system-ui,sans-serif', "Rubik", "300;400;500;600;700;800"),
    ("space-grotesk", "Space Grotesk", '"Space Grotesk",system-ui,sans-serif', "Space Grotesk", "300;400;500;600;700"),
    ("oswald", "Oswald", '"Oswald",Impact,sans-serif', "Oswald", "300;400;500;600;700"),
    ("playfair", "Playfair Display", '"Playfair Display",Georgia,serif', "Playfair Display", "400;500;600;700;800"),
    ("merriweather", "Merriweather", '"Merriweather",Georgia,serif', "Merriweather", "300;400;700"),
    ("lora", "Lora", '"Lora",Georgia,serif', "Lora", "400;500;600;700"),
    ("pt-serif", "PT Serif", '"PT Serif",Georgia,serif', "PT Serif", "400;700"),
    ("libre-baskerville", "Libre Baskerville", '"Libre Baskerville",Georgia,serif', "Libre Baskerville", "400;700"),
]
FONT_KEYS = {f[0] for f in FONTS}


def _clean_font(spec, value, where):
    if value in ("", None):
        return ""
    if value not in FONT_KEYS:
        raise serializers.ValidationError(f"{where} is not one of the available fonts.")
    return value


def _clean_font_size(spec, value, where):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise serializers.ValidationError(f"{where} must be a number.")
    value = int(value)
    if value != 0 and not spec["min"] <= value <= spec["max"]:
        raise serializers.ValidationError(f"{where} must be between {spec['min']} and {spec['max']}, or 0 for the theme size.")
    return value


VALUE_CLEANERS.update({"font": _clean_font, "font_size": _clean_font_size})

_WEIGHTS = ["", "300", "400", "500", "600", "700", "800"]
_WEIGHT_LABELS = {"": "Theme default", "300": "Light", "400": "Regular", "500": "Medium", "600": "Semi-bold", "700": "Bold", "800": "Extra bold"}
_TRANSFORMS = ["", "none", "uppercase", "lowercase", "capitalize"]
_TRANSFORM_LABELS = {"": "Theme default", "none": "As typed", "uppercase": "UPPERCASE", "lowercase": "lowercase", "capitalize": "Capitalize"}
TEXT_PRESETS = {"Small": 14, "Medium": 16, "Large": 20, "Extra large": 28}
HEADING_PRESETS = {"Small": 20, "Medium": 28, "Large": 40, "Extra large": 56}


def _td(group, type_, label, default, **extra):
    """An option that follows the theme until changed."""
    return _opt(group, type_, label, default, theme_default=True, **extra)


def text_settings(presets=TEXT_PRESETS):
    """Text options for a block: font, size, weight, style, colour, spacing, alignment, shadow, links."""
    return {
        "t_font": _td("text", "font", "Font", ""),
        "t_size": _td("text", "font_size", "Font size (desktop)", 0, min=10, max=96, unit="px", presets=presets,
                      help="Theme uses the theme's size."),
        "t_size_mobile": _td("text", "font_size", "Font size (mobile)", 0, min=10, max=96, unit="px", presets=presets),
        "t_weight": _td("text", "select", "Font weight", "", options=_WEIGHTS, labels=_WEIGHT_LABELS),
        "t_italic": _td("text", "checkbox", "Italic", False),
        "t_underline": _td("text", "checkbox", "Underline", False),
        "t_strike": _td("text", "checkbox", "Strikethrough", False),
        "t_transform": _td("text", "select", "Letter case", "", options=_TRANSFORMS, labels=_TRANSFORM_LABELS),
        "t_color": _td("text", "color_optional", "Text colour", ""),
        "t_line_height": _td(
            "text", "select", "Line height", "", options=["", "1", "1.2", "1.4", "1.6", "1.8", "2"],
            labels={"": "Theme default", "1": "1 (tight)", "1.6": "1.6 (normal)", "2": "2 (loose)"},
        ),
        "t_letter_spacing": _td("text", "range", "Letter spacing", 0, min=-2, max=12, unit="px"),
        "t_word_spacing": _td("text", "range", "Word spacing", 0, min=0, max=24, unit="px"),
        "t_align": _td(
            "text", "select", "Text alignment", "", options=["", "left", "center", "right", "justify"],
            labels={"": "Auto"}, ui="segmented",
        ),
        "t_shadow": _td("text", "select", "Text shadow", "none", options=["none", "soft", "strong"], ui="segmented"),
        "t_link_color": _td("text", "color_optional", "Link colour", ""),
        "t_link_hover": _td("text", "checkbox", "Underline links only on hover", False,
                            help="Off: links are always underlined."),
    }


def section_text_settings():
    """Text options for a whole section: its fonts, shadow and links."""
    return {
        "t_heading_font": _td("text", "font", "Heading font", ""),
        "t_body_font": _td("text", "font", "Body font", ""),
        "t_shadow": _td("text", "select", "Text shadow", "none", options=["none", "soft", "strong"], ui="segmented"),
        "t_link_color": _td("text", "color_optional", "Link colour", ""),
        "t_link_hover": _td("text", "checkbox", "Underline links only on hover", False),
    }


BUTTON_ICONS = ["", "arrow", "cart", "bag", "heart", "star", "check", "external", "mail", "phone", "download"]


def button_settings():
    """Button style, colours, size, shape, typography, effects and alignment."""
    return {
        "b_icon": _td("button", "select", "Icon", "", options=BUTTON_ICONS, labels={"": "No icon", "external": "External link"}),
        "b_icon_position": _td("button", "select", "Icon position", "right", options=["left", "right"], ui="segmented",
                               show_if={"b_icon": "*"}),
        "b_style": _td(
            "button", "select", "Style", "", options=["", "filled", "outline", "soft", "text", "link"],
            labels={"": "Theme default", "soft": "Soft (light background)", "text": "Text only", "link": "Link style"},
        ),
        "b_bg": _td("button", "color_optional", "Background colour", ""),
        "b_text": _td("button", "color_optional", "Text colour", ""),
        "b_border": _td("button", "color_optional", "Border colour", ""),
        "b_hover_bg": _td("button", "color_optional", "Hover background", ""),
        "b_hover_text": _td("button", "color_optional", "Hover text colour", ""),
        "b_hover_border": _td("button", "color_optional", "Hover border colour", ""),
        "b_size": _td("button", "select", "Size", "", options=["", "small", "medium", "large"], labels={"": "Theme"}, ui="segmented"),
        "b_height": _td("button", "number", "Custom height", 0, min=0, max=96, unit="px", help="0 uses the size above."),
        "b_padding_x": _td("button", "number", "Side padding", 0, min=0, max=80, unit="px", help="0 uses the size above."),
        "b_radius": _td(
            "button", "select", "Corners", "", options=["", "square", "slight", "rounded", "pill"],
            labels={"": "Theme", "slight": "Slight"}, ui="segmented",
        ),
        "b_border_width": _td("button", "range", "Border width", 1, min=0, max=4, unit="px"),
        "b_full_width": _td("button", "checkbox", "Full width", False),
        "b_font": _td("button", "font", "Font", ""),
        "b_font_size": _td("button", "font_size", "Font size", 0, min=10, max=40, unit="px",
                           presets={"Small": 13, "Medium": 15, "Large": 17, "Extra large": 20}),
        "b_weight": _td("button", "select", "Font weight", "", options=_WEIGHTS, labels=_WEIGHT_LABELS),
        "b_transform": _td("button", "select", "Letter case", "", options=_TRANSFORMS, labels=_TRANSFORM_LABELS),
        "b_shadow": _td("button", "select", "Shadow", "none", options=["none", "soft", "strong"], ui="segmented"),
        "b_hover": _td(
            "button", "select", "Hover effect", "", options=["", "none", "lift", "darken", "grow"],
            labels={"": "Theme default"},
        ),
        "b_speed": _td("button", "range", "Transition speed", 150, min=0, max=600, step=50, unit="ms"),
        "b_align": _td("button", "select", "Button alignment", "", options=["", "left", "center", "right"],
                       labels={"": "Auto"}, ui="segmented"),
        "b_gap": _td("button", "range", "Space between buttons", 12, min=0, max=48, step=2, unit="px",
                     help="When two buttons sit side by side."),
    }


# Theme-wide defaults new sections and blocks start with
THEME_SETTINGS.update({
    "heading_font": {"type": "font", "label": "Heading font", "default": "system"},
    "body_font": {"type": "font", "label": "Body font", "default": "system"},
    "base_font_size": {"type": "number", "label": "Base font size", "default": 16, "min": 12, "max": 22, "unit": "px",
                       "help": "The size of normal text across the store."},
    "button_style": {"type": "select", "label": "Button style", "default": "filled",
                     "options": ["filled", "outline", "soft", "text", "link"],
                     "labels": {"soft": "Soft (light background)", "text": "Text only", "link": "Link style"}},
    "button_bg": {"type": "color_optional", "label": "Button colour", "default": "", "help": "None uses the accent colour."},
    "button_text": {"type": "color_optional", "label": "Button text colour", "default": ""},
    "button_radius": {"type": "select", "label": "Button corners", "default": "pill", "ui": "segmented",
                      "options": ["square", "slight", "rounded", "pill"], "labels": {"slight": "Slight"}},
    "button_hover": {"type": "select", "label": "Button hover effect", "default": "darken",
                     "options": ["none", "lift", "darken", "grow"]},
})


# ---------------------------------------------------------------------------
# Page layout: announcement bar, header (logo + navigation) and footer. These
# are theme-wide, so they live in THEME_SETTINGS and show in the customizer's
# "Theme settings" panel; layout/theme.liquid and the header/footer snippets
# read them.
# ---------------------------------------------------------------------------

MENU_MAX_LINKS = 12
_LINK_STARTS = ("/", "https://", "http://", "mailto:", "tel:", "#")


def _clean_menu(spec, value, where):
    """One link per line: "Label | /page" (a bare "/page" or address uses itself as the label)."""
    if not isinstance(value, str):
        raise serializers.ValidationError(f"{where} must be text.")
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if len(lines) > MENU_MAX_LINKS:
        raise serializers.ValidationError(f"{where} can have at most {MENU_MAX_LINKS} links.")
    for number, line in enumerate(lines, 1):
        label, _, url = line.partition("|")
        label, url = label.strip(), url.strip()
        if not url and label.startswith(_LINK_STARTS):  # a bare link is its own label
            label, url = label, label
        if not url or not label:
            raise serializers.ValidationError(f"{where}, line {number}: write it as  Label | /page")
        if len(label) > 60:
            raise serializers.ValidationError(f"{where}, line {number}: the label is too long.")
        if not url.startswith(_LINK_STARTS) or url.startswith("//"):
            raise serializers.ValidationError(
                f"{where}, line {number}: the link must be a page (/...), a web address, mailto:, tel: or #section."
            )
    return "\n".join(lines)


VALUE_CLEANERS["menu"] = _clean_menu


def _theme_opt(group, type_, label, default, **extra):
    return {"type": type_, "label": label, "default": default, "group": group, **extra}


THEME_SETTINGS.update({
    "announcement_show": _theme_opt("announcement", "checkbox", "Show the announcement bar", True),
    "announcement_link": _theme_opt("announcement", "url", "Link (optional)", "", help="Makes the whole bar a link."),
    "announcement_bg": _theme_opt("announcement", "color_optional", "Bar colour", "", help="None uses the accent colour."),
    "announcement_text": _theme_opt("announcement", "color_optional", "Text colour", ""),
    "header_layout": _theme_opt(
        "header", "select", "Logo position", "left", options=["left", "center"], ui="segmented",
        labels={"left": "Left", "center": "Centred"},
    ),
    "header_logo_text": _theme_opt("header", "text", "Logo text", "", max=60, help="Empty uses the store name."),
    "header_logo_url": _theme_opt("header", "image", "Logo image", "", help="Optional. Shown instead of the logo text."),
    "header_logo_height": _theme_opt("header", "range", "Logo image height", 36, min=16, max=96, step=2, unit="px"),
    "header_show_nav": _theme_opt("header", "checkbox", "Show the navigation menu", True),
    "header_menu": _theme_opt(
        "header", "menu", "Menu links", "",
        help="One link per line: Label | /page. Leave empty for Home, Shop all and your collections.",
    ),
    "header_collections": _theme_opt(
        "header", "range", "Collections in the automatic menu", 4, min=0, max=8,
        help="Used only when the menu links above are empty.",
    ),
    "header_sticky": _theme_opt("header", "checkbox", "Keep the header at the top while scrolling", True),
    "header_border": _theme_opt("header", "checkbox", "Line under the header", True),
    "header_bg": _theme_opt("header", "color_optional", "Header colour", "", help="None uses the background colour."),
    "header_text": _theme_opt("header", "color_optional", "Header text colour", ""),
    "footer_text": _theme_opt("footer", "textarea", "About text", "", max=600, help="Shown at the top of the footer."),
    "footer_show_links": _theme_opt("footer", "checkbox", "Show the navigation links", True),
    "footer_menu": _theme_opt(
        "footer", "menu", "Footer links", "",
        help="One link per line: Label | /page. Leave empty for Home and Shop all.",
    ),
    "footer_instagram": _theme_opt("footer", "url", "Instagram", ""),
    "footer_facebook": _theme_opt("footer", "url", "Facebook", ""),
    "footer_x": _theme_opt("footer", "url", "X (Twitter)", ""),
    "footer_tiktok": _theme_opt("footer", "url", "TikTok", ""),
    "footer_youtube": _theme_opt("footer", "url", "YouTube", ""),
    "footer_pinterest": _theme_opt("footer", "url", "Pinterest", ""),
    "footer_linkedin": _theme_opt("footer", "url", "LinkedIn", ""),
    "footer_copyright": _theme_opt(
        "footer", "text", "Copyright line", "", max=200, help="Empty shows © the year and the store name.",
    ),
    "footer_bg": _theme_opt("footer", "color_optional", "Footer colour", "", help="None uses the background colour."),
    "footer_text_color": _theme_opt("footer", "color_optional", "Footer text colour", ""),
})

# Where each theme-wide setting appears in the "Theme settings" panel
_THEME_GROUP_OF = {
    "accent_color": "colors", "background_color": "colors", "text_color": "colors",
    "currency_symbol": "store", "announcement": "announcement",
    "heading_font": "typography", "body_font": "typography", "base_font_size": "typography",
    "button_style": "buttons", "button_bg": "buttons", "button_text": "buttons",
    "button_radius": "buttons", "button_hover": "buttons",
}
for _key, _spec in THEME_SETTINGS.items():
    _spec.setdefault("group", _THEME_GROUP_OF.get(_key, "store"))

THEME_GROUPS = [
    {"id": "colors", "label": "Colours"},
    {"id": "typography", "label": "Typography"},
    {"id": "buttons", "label": "Buttons"},
    {"id": "announcement", "label": "Announcement bar"},
    {"id": "header", "label": "Header and navigation"},
    {"id": "footer", "label": "Footer"},
    {"id": "blog", "label": "Blog"},
    {"id": "store", "label": "Store"},
]


def _insert_before_layout(settings, extra):
    """Put `extra` options after the section/block's content options, before layout."""
    first = [(k, v) for k, v in settings.items() if v.get("group", "content") == "content"]
    rest = [(k, v) for k, v in settings.items() if v.get("group", "content") != "content"]
    return {**dict(first), **extra, **dict(rest)}


# Blocks: text options on text blocks, button options on the button block
for _key in ("heading", "text", "quote", "list", "icon_text", "accordion", "countdown", "social"):
    BLOCKS[_key]["settings"] = _insert_before_layout(
        BLOCKS[_key]["settings"], text_settings(HEADING_PRESETS if _key == "heading" else TEXT_PRESETS),
    )
# Slides: text options for the slide's heading, sub-text and links
SECTIONS["slideshow"]["blocks"]["slide"]["settings"] = _insert_before_layout(
    SECTIONS["slideshow"]["blocks"]["slide"]["settings"], text_settings(HEADING_PRESETS),
)
_button = {k: v for k, v in BLOCKS["button"]["settings"].items() if k != "style"}
BLOCKS["button"]["settings"] = _insert_before_layout(_button, button_settings())

# Sections: fonts and links for every section, button options where there are buttons
for _type, _schema in SECTIONS.items():
    extra = section_text_settings()
    if _type in ("hero", "slideshow", "rich-text", "custom", "product-carousel", "product-grid", "main-product", "main-collection"):
        extra.update(button_settings())
    settings = {k: v for k, v in _schema["settings"].items() if not (_type == "hero" and k in ("button_style", "button_color"))}
    _schema["settings"] = _insert_before_layout(settings, extra)

GROUPS[:] = [
    {"id": "content", "label": "Content"},
    {"id": "text", "label": "Text"},
    {"id": "button", "label": "Button"},
    {"id": "layout", "label": "Layout"},
    {"id": "style", "label": "Style"},
    {"id": "advanced", "label": "Advanced"},
]
