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


def _clean_value(spec, value, where):
    kind = spec["type"]

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

    section = {
        "type": raw["type"],
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
                s["type"] == type_ for s in cleaned.values()
            ):
                raise serializers.ValidationError(f"templates.{page} must include a {schema['name']} section.")

        clean["templates"][page] = {"order": order, "sections": cleaned}

    return clean


def _with_defaults(schema, values):
    return {key: values.get(key, spec["default"]) for key, spec in schema.items()}


def resolve(custom_data):
    """custom_data merged over the defaults: what the renderer uses."""
    custom_data = custom_data or {}
    templates = copy.deepcopy(DEFAULT_TEMPLATES)
    templates.update(copy.deepcopy(custom_data.get("templates", {})))

    for page in templates.values():
        for section in page["sections"].values():
            schema = SECTIONS[section["type"]]
            section["settings"] = _with_defaults(schema["settings"], section.get("settings", {}))
            if "blocks" in schema:
                section["blocks"] = [
                    {
                        "type": b["type"],
                        "settings": _with_defaults(schema["blocks"][b["type"]]["settings"], b.get("settings", {})),
                    }
                    for b in section.get("blocks", [])
                ]

    return {
        "settings": _with_defaults(THEME_SETTINGS, custom_data.get("settings", {})),
        "templates": templates,
    }


def public_schema():
    """Schemas for the admin customizer UI."""
    return {"settings": THEME_SETTINGS, "sections": SECTIONS, "pages": list(PAGES)}
