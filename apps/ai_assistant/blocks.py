"""
What the assistant may build, generated from apps/themes/schemas.py (SECTIONS and BLOCKS): the tool parameter
schemas, the catalog in the system prompt and the validation all read the same definitions, so a new setting or
block type in the theme is picked up here without a second schema to keep in step.

The assistant works on the base "page" templates only (About, FAQ, Contact...). It does not touch the home, product
or collection templates (planned for v2).
"""
import re

from rest_framework import serializers

from apps.themes import schemas

# Section types the assistant may place on a store page, and when each is the right one.
AI_SECTIONS = {
    "hero": "A big banner with a heading, a line of text, an image and up to two buttons. Use at the top of a page.",
    "rich-text": "A heading plus a paragraph of text, with an optional image and button. Use for an 'our story' block.",
    "custom": (
        "A flexible section that holds any blocks one under another (headings, text, quotes, lists, accordions, "
        "icons, images, buttons, maps). Use it for testimonials, FAQ, features, contact details."
    ),
    "slideshow": "Rotating slides, each with an image, heading, text and button.",
    "newsletter": "An email sign-up form with a heading.",
    "featured-posts": "The latest blog posts.",
}

# Block types allowed inside the sections above (a subset of schemas.BLOCKS plus the slideshow's slide).
AI_BLOCKS = (
    "slide", "heading", "text", "image", "button", "video", "products", "collections", "divider", "spacer",
    "icon_text", "list", "quote", "accordion", "map", "html", "icon", "product_card", "collection_card",
    "collection_link", "jumbo_text",
)

BODY_SECTION = "main-page"  # shows the page's title and body; the assistant never adds, moves or removes it
MAX_AI_SECTIONS = 12
_ID = re.compile(r"[^a-z0-9]+")


def _content(settings):
    """The settings a merchant's words go in; the style groups (fonts, colors, spacing) are left at the theme's defaults."""
    return {key: spec for key, spec in settings.items() if spec.get("group") in (None, "content")}


def _block_defs(section_type):
    return {
        name: spec for name, spec in schemas.SECTIONS[section_type].get("blocks", {}).items()
        if name in AI_BLOCKS
    }


def section_types():
    return [name for name in AI_SECTIONS if name in schemas.SECTIONS]


def block_types():
    """Every block type any allowed section accepts, in a stable order."""
    seen = []
    for section in section_types():
        for name in _block_defs(section):
            if name not in seen:
                seen.append(name)
    return seen


def _field(spec):
    """One setting as the model sees it."""
    kind = spec["type"]
    out = {"default": spec.get("default")}
    if kind == "select":
        out["type"], out["options"] = "one of", list(spec["options"])
    elif kind in ("range", "number"):
        out["type"], out["min"], out["max"] = "integer", spec.get("min"), spec.get("max")
    elif kind == "checkbox":
        out["type"] = "true/false"
    elif kind == "richtext":
        out["type"] = "HTML text (p, br, strong, em, ul, ol, li, a, h3, h4, blockquote)"
    elif kind == "embed":
        out["type"] = "HTML"
    elif kind in ("image", "url"):
        out["type"] = "link starting with /, https:// (or mailto:, tel:, # for links)"
    else:
        out["type"] = "text"
    if "max" in spec and kind not in ("range", "number"):
        out["max_length"] = spec["max"]
    if spec.get("help"):
        out["help"] = spec["help"]
    if spec.get("label"):
        out["label"] = spec["label"]
    return out


def describe_section_type(name):
    """Everything the assistant may set on one section type (the describe_section_type tool's answer)."""
    if name not in AI_SECTIONS or name not in schemas.SECTIONS:
        raise ValueError(f"Unknown section type '{name}'. Use one of: {', '.join(section_types())}.")
    schema = schemas.SECTIONS[name]
    out = {
        "type": name,
        "name": schema["name"],
        "use_for": AI_SECTIONS[name],
        "settings": {key: _field(spec) for key, spec in _content(schema["settings"]).items()},
    }
    blocks = _block_defs(name)
    if blocks:
        out["max_blocks"] = schema.get("max_blocks")
        out["blocks"] = {
            key: {"name": spec["name"], "settings": {k: _field(v) for k, v in _content(spec["settings"]).items()}}
            for key, spec in blocks.items()
        }
    return out


def catalog_text():
    """The block catalog for the system prompt."""
    lines = ["Section types:"]
    for name in section_types():
        lines.append(f"- {name} ({schemas.SECTIONS[name]['name']}): {AI_SECTIONS[name]}")
    lines.append("Block types (inside 'custom', 'rich-text', 'hero' and other sections that accept blocks):")
    for name in block_types():
        if name == "slide":
            lines.append("- slide (Slide): only inside a slideshow section.")
            continue
        spec = schemas.BLOCKS.get(name)
        if spec:
            fields = ", ".join(_content(spec["settings"]))
            lines.append(f"- {name} ({spec['name']}): {fields}")
    return "\n".join(lines)


# ----------------------------------------------------------- the tool parameter schemas

def section_json_schema():
    return {
        "type": "object",
        "description": "A page section. Call describe_section_type first if unsure which settings it accepts.",
        "properties": {
            "type": {"type": "string", "enum": section_types()},
            "settings": {"type": "object", "description": "Setting name -> value, as described by describe_section_type."},
            "blocks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": block_types()},
                        "settings": {"type": "object"},
                    },
                    "required": ["type"],
                },
            },
            "hidden": {"type": "boolean"},
        },
        "required": ["type"],
    }


def tool_definitions():
    """The tools in the OpenAI function-calling format."""
    section = section_json_schema()

    def fn(name, description, properties=None, required=()):
        return {"type": "function", "function": {
            "name": name, "description": description,
            "parameters": {"type": "object", "properties": properties or {}, "required": list(required)},
        }}

    page_id = {"type": "string", "description": "The page's id from list_pages."}
    return [
        fn("list_pages", "List this store's pages (id, title, handle, status). Only draft (hidden) pages can be edited."),
        fn("get_page", "Get one page with its sections (ids, types, settings, blocks).", {"page_id": page_id}, ["page_id"]),
        fn("describe_section_type", "The settings and blocks one section type accepts.",
           {"type": {"type": "string", "enum": section_types()}}, ["type"]),
        fn("create_page", "Create a new DRAFT page (hidden until the merchant publishes it) with sections in order.", {
            "title": {"type": "string"},
            "slug": {"type": "string", "description": "URL handle, e.g. about-us. Optional; made from the title."},
            "sections": {"type": "array", "items": section},
            "body": {"type": "string", "description": "Optional page text as simple HTML."},
        }, ["title", "sections"]),
        fn("add_section", "Add a section to a draft page.", {
            "page_id": page_id, "section": section,
            "position": {"type": "integer", "description": "0 = top. Omit to add at the end."},
        }, ["page_id", "section"]),
        fn("update_section", "Change a section: settings are merged into the current ones; 'blocks', if given, replace all blocks.", {
            "page_id": page_id,
            "section_id": {"type": "string"},
            "changes": {"type": "object", "properties": {
                "settings": {"type": "object"}, "blocks": section["properties"]["blocks"], "hidden": {"type": "boolean"},
            }},
        }, ["page_id", "section_id", "changes"]),
        fn("delete_section", "Remove a section from a draft page.",
           {"page_id": page_id, "section_id": {"type": "string"}}, ["page_id", "section_id"]),
        fn("list_collections", "The store's real collections (id, name, handle). Use these, never invent names."),
        fn("list_products", "The store's real products (id, title, handle). Use these, never invent names.", {
            "search": {"type": "string", "description": "Optional text to look for in the title."},
            "limit": {"type": "integer"},
        }),
    ]


# ----------------------------------------------------------- validation

class BlockError(ValueError):
    """The model's section is not valid; the message goes back to the model so it can fix it."""


def _message(error):
    detail = error.detail
    if isinstance(detail, dict):
        detail = [f"{k}: {v}" for k, v in detail.items()]
    if not isinstance(detail, (list, tuple)):
        detail = [detail]
    return " ".join(str(item) for item in detail)


def validate_template(page_key, template):
    """
    Check a page template ({"order": [...], "sections": {id: {...}}}) against the theme's own schema, plus the
    assistant's limits. Returns the cleaned template (HTML sanitized, defaults kept). Raises BlockError.
    """
    sections = template.get("sections") if isinstance(template, dict) else None
    if not isinstance(sections, dict):
        raise BlockError("Expected sections.")
    ai_count = 0
    for section_id, section in sections.items():
        if not isinstance(section, dict):
            raise BlockError(f"Section '{section_id}' must be an object.")
        kind = section.get("type")
        if kind == BODY_SECTION:
            continue
        ai_count += 1
        if kind not in AI_SECTIONS:
            raise BlockError(f"Unknown section type '{kind}'. Use one of: {', '.join(section_types())}.")
        allowed = _block_defs(kind)
        for i, block in enumerate(section.get("blocks") or []):
            btype = block.get("type") if isinstance(block, dict) else None
            if btype not in allowed:
                raise BlockError(
                    f"Section '{kind}' block {i}: unknown block type '{btype}'. "
                    f"Allowed here: {', '.join(allowed)}."
                )
    if ai_count > MAX_AI_SECTIONS:
        raise BlockError(f"A page can have at most {MAX_AI_SECTIONS} sections built by the assistant.")
    try:
        clean = schemas.validate_custom_data({"templates": {page_key: template}})
    except serializers.ValidationError as error:
        raise BlockError(_message(error)) from error
    return clean["templates"][page_key]


def new_section_id(kind, taken):
    base = _ID.sub("-", kind).strip("-")[:30] or "section"
    n, candidate = 1, base
    while candidate in taken:
        n += 1
        candidate = f"{base}-{n}"
    return candidate
