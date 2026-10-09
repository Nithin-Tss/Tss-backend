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

PAGES = ("index", "product", "collection")

THEME_SETTINGS = {
    "accent_color": {"type": "color", "label": "Accent color", "default": "#161C2C"},
    "background_color": {"type": "color", "label": "Background color", "default": "#FFFFFF"},
    "text_color": {"type": "color", "label": "Text color", "default": "#161C2C"},
    "currency_symbol": {"type": "text", "label": "Currency symbol", "default": "₹", "max": 4},
    "announcement": {"type": "text", "label": "Announcement bar", "default": ""},
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
        "pages": PAGES,
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
        "pages": PAGES,
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
            "featured": {"type": "product-grid", "settings": {}},
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
    if kind in ("url", "image") and value and not value.startswith(("/", "https://", "http://")):
        raise serializers.ValidationError(f"{where} must start with /, http:// or https://.")
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

    if page not in schema["pages"]:
        raise serializers.ValidationError(f"{where}: {schema['name']} can't be used on the {page} page.")

    # Hidden sections stay saved but aren't shown on the store
    hidden = raw.get("hidden", False)
    if not isinstance(hidden, bool):
        raise serializers.ValidationError(f"{where}.hidden must be true or false.")

    section = {
        "type": raw["type"],
        "hidden": hidden,
        "settings": _clean_settings(schema["settings"], raw.get("settings", {}), f"{where}.settings"),
    }

    if "blocks" in schema:
        blocks = raw.get("blocks", [])
        if not isinstance(blocks, list) or len(blocks) > schema["max_blocks"]:
            raise serializers.ValidationError(f"{where}.blocks must be a list of at most {schema['max_blocks']}.")

        section["blocks"] = []
        for i, block in enumerate(blocks):
            if not isinstance(block, dict) or block.get("type") not in schema["blocks"]:
                raise serializers.ValidationError(f"{where}.blocks[{i}].type is not valid.")
            block_schema = schema["blocks"][block["type"]]
            section["blocks"].append({
                "type": block["type"],
                "settings": _clean_settings(
                    block_schema["settings"], block.get("settings", {}), f"{where}.blocks[{i}].settings"
                ),
            })

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

    if not isinstance(templates, dict) or set(templates) - set(PAGES):
        raise serializers.ValidationError(f"templates may only contain: {', '.join(PAGES)}.")

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
            if schema.get("required") and page in schema["pages"] and not any(
                s["type"] == type_ and not s["hidden"] for s in cleaned.values()
            ):
                raise serializers.ValidationError(f"templates.{page} must include a {schema['name']} section.")

        clean["templates"][page] = {"order": order, "sections": cleaned}

    return clean


def _with_defaults(schema, values):
    return {key: values.get(key, copy.deepcopy(spec["default"])) for key, spec in schema.items()}


def resolve(custom_data):
    """custom_data merged over the defaults: what the renderer uses."""
    custom_data = custom_data or {}
    templates = copy.deepcopy(DEFAULT_TEMPLATES)
    templates.update(copy.deepcopy(custom_data.get("templates", {})))

    for page in templates.values():
        for section in page["sections"].values():
            schema = SECTIONS[section["type"]]
            section["hidden"] = bool(section.get("hidden", False))
            section["settings"] = _with_defaults(schema["settings"], section.get("settings", {}))
            if "blocks" in schema:
                section["blocks"] = [
                    {
                        "type": b["type"],
                        "settings": _with_defaults(schema["blocks"][b["type"]]["settings"], b.get("settings", {})),
                    }
                    for b in section.get("blocks", [])
                    if b.get("type") in schema["blocks"]  # block types that no longer exist are dropped
                ]

    return {
        "settings": _with_defaults(THEME_SETTINGS, custom_data.get("settings", {})),
        "templates": templates,
    }


def public_schema():
    """Schemas for the admin customizer UI."""
    return {"settings": THEME_SETTINGS, "sections": SECTIONS, "pages": list(PAGES)}


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


# ---------------------------------------------------------------------------
# Page builder: ready-made sections get "Section heading" labels, the
# slideshow gets a heading, Featured products can be picked one by one,
# Rich text gets an editor.
# ---------------------------------------------------------------------------

_hero = SECTIONS["hero"]["settings"]
SECTIONS["hero"]["settings"] = {
    "heading": dict(_hero["heading"], label="Section heading"),
    "subheading": dict(_hero["subheading"], label="Sub-text"),
    "button_label": dict(_hero["button_label"], label="Button text"),
    "button_link": _hero["button_link"],
    "image_url": dict(_hero["image_url"], label="Background image"),
    **{k: v for k, v in _hero.items() if k not in ("heading", "subheading", "button_label", "button_link", "image_url")},
}

SECTIONS["slideshow"]["settings"] = {
    "heading": {"type": "text", "label": "Section heading", "default": ""},
    **SECTIONS["slideshow"]["settings"],
}
_slide = SECTIONS["slideshow"]["blocks"]["slide"]["settings"]
_slide["image_url"] = dict(_slide["image_url"], label="Image")

_carousel = SECTIONS["product-carousel"]["settings"]
_carousel["heading"] = dict(_carousel["heading"], label="Section heading")

_grid = SECTIONS["product-grid"]["settings"]
SECTIONS["product-grid"]["settings"] = {
    "heading": dict(_grid["heading"], label="Section heading"),
    "products": {
        "type": "products",
        "label": "Products to show (none chosen = from the collection below)",
        "default": [],
        "max": 24,
    },
    **{k: v for k, v in _grid.items() if k != "heading"},
}

_rich = SECTIONS["rich-text"]["settings"]
_rich["heading"] = dict(_rich["heading"], label="Section heading")
_rich["text"] = {"type": "richtext", "label": "Body", "default": "<p>Tell customers about your brand.</p>", "max": 20000}

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
