"""
The tools the model can call. Each one works on ctx.store only: the store comes from the signed-in member's
request, never from the model's arguments, so no prompt can point a tool at another merchant's data.

v1 scope: standalone store pages (StorePage) and their own page template. Everything built here stays a hidden
draft; publishing is a separate, merchant-only step (see services.publish_draft). Home / product / collection
templates are not reachable from any tool (v2: section-level drafts).
"""
import copy
import json
import logging
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils.text import slugify

from apps.catalog.models import Collection, Product
from apps.themes import schemas
from apps.themes.models import StorePage, StoreTheme
from apps.themes.services import (
    create_template_for_page, get_store_theme, page_template_keys, templates_changed,
)

from . import blocks
from .models import AiPageDraft

logger = logging.getLogger(__name__)

LIST_LIMIT = 50


class ToolError(Exception):
    """The call was refused; the message goes back to the model. kind: validation | malformed | refused | unknown."""

    def __init__(self, message, kind="validation"):
        super().__init__(message)
        self.kind = kind


@dataclass
class Context:
    store: object
    user: object
    conversation: object = None
    changes: list = field(default_factory=list)


# ------------------------------------------------------------------ helpers

def _args_str(args, key, required=True, limit=255):
    value = args.get(key)
    if value is None or value == "":
        if required:
            raise ToolError(f"'{key}' is required.")
        return ""
    if not isinstance(value, str):
        raise ToolError(f"'{key}' must be text.")
    if len(value) > limit:
        raise ToolError(f"'{key}' is too long (at most {limit} characters).")
    return value.strip()


def _page(ctx, page_id):
    """The store's page, or a refusal. The store filter is what keeps merchants apart."""
    try:
        page = StorePage.objects.filter(store=ctx.store, pk=page_id).first()
    except (ValueError, TypeError, DjangoValidationError):  # not a uuid
        page = None
    if page is None:
        raise ToolError("No such page in this store. Call list_pages for the real ids.", "refused")
    return page


def _editable_page(ctx, page_id):
    page = _page(ctx, page_id)
    if page.status != StorePage.HIDDEN:
        raise ToolError(
            f"'{page.title}' is published, so the assistant can't change it. "
            "Create a new draft page instead, or ask the merchant to hide this page first.", "refused",
        )
    return page


def _store_theme(ctx):
    return get_store_theme(ctx.store)


def _templates(store_theme):
    return (store_theme.custom_data or {}).get("templates") or {}


def _record(ctx, action, page, summary):
    ctx.changes.append({
        "action": action, "page_id": str(page.pk), "title": page.title, "handle": page.handle,
        "template": page.template, "summary": summary,
    })


def _ensure_draft(ctx, page, created=False):
    """Remember a page the assistant touches (and what it looked like), so it can be published or discarded."""
    existing = AiPageDraft.objects.filter(store=ctx.store, page=page).first()
    if existing:
        return existing
    original = {}
    if not created:
        data = _templates(_store_theme(ctx)).get(page.template)
        original = {"title": page.title, "template": page.template, "template_data": copy.deepcopy(data)}
    return AiPageDraft.objects.create(
        store=ctx.store, page=page, conversation=ctx.conversation, created_by_ai=created, original=original,
    )


def _own_template(ctx, page):
    """The key of a template only this page uses, made (as a copy of the Page template) when it has none."""
    store_theme = _store_theme(ctx)
    key = page.template
    shared = StorePage.objects.filter(store=ctx.store, template=key).exclude(pk=page.pk).exists()
    if key and key in page_template_keys(store_theme.custom_data) and not shared:
        return store_theme, key
    new_key = create_template_for_page(store_theme, page)
    if not new_key:
        raise ToolError("This theme has reached its limit of page templates.", "refused")
    page.template = new_key
    page.save(update_fields=["template"])
    store_theme.refresh_from_db()
    return store_theme, new_key


def _check_references(ctx, template):
    """Products and collections a section points at must be this store's own."""
    wanted = {"products": set(), "collections": set()}
    for section in template["sections"].values():
        schema = schemas.SECTIONS.get(section["type"])
        if not schema:
            continue
        for key, value in section.get("settings", {}).items():
            kind = schema["settings"].get(key, {}).get("type")
            if kind in wanted:
                wanted[kind].update(value)
        for block in section.get("blocks", []):
            specs = schema.get("blocks", {}).get(block["type"], {}).get("settings", {})
            for key, value in block.get("settings", {}).items():
                kind = specs.get(key, {}).get("type")
                if kind in wanted:
                    wanted[kind].update(value)
    for model, kind in ((Product, "products"), (Collection, "collections")):
        ids = wanted[kind]
        if ids:
            found = {str(pk) for pk in model.objects.filter(store=ctx.store, pk__in=ids).values_list("pk", flat=True)}
            missing = ids - found
            if missing:
                raise ToolError(
                    f"Unknown {kind[:-1]} id(s): {', '.join(sorted(missing))}. "
                    f"Use ids from list_{kind}.",
                )


def _write_template(ctx, store_theme, key, template):
    """Validate and save one page template into the store's published theme (reachable only through this page)."""
    if not key or key in schemas.PAGES or key not in page_template_keys(store_theme.custom_data):
        raise ToolError("That template can't be edited by the assistant.", "refused")
    try:
        clean = blocks.validate_template(key, template)
    except blocks.BlockError as error:
        raise ToolError(str(error)) from error
    _check_references(ctx, clean)

    store_theme.refresh_from_db(fields=["custom_data"])
    before = schemas.resolve(store_theme.custom_data)["templates"]
    merged = schemas.resolve(copy.deepcopy(store_theme.custom_data or {}))
    merged["templates"][key] = clean
    merged.pop("sections", None)
    full = schemas.validate_custom_data(merged)
    # The synced sections (header, footer) keep their newest saved values, as in the customizer's own save
    fresh = StoreTheme.objects.filter(pk=store_theme.pk).values_list("custom_data", flat=True).first() or {}
    if fresh.get("sections"):
        full["sections"] = fresh["sections"]
    store_theme.custom_data = full
    store_theme.save(update_fields=["custom_data", "updated_at"])
    templates_changed(store_theme, before, client="ai-assistant", source="ai")
    return clean


def _section_view(section_id, section):
    out = {"id": section_id, "type": section["type"], "settings": section.get("settings", {})}
    if section.get("hidden"):
        out["hidden"] = True
    if section.get("blocks"):
        out["blocks"] = [{"index": i, "type": b["type"], "settings": b.get("settings", {})}
                         for i, b in enumerate(section["blocks"])]
    return out


def _build_sections(raw_sections):
    if not isinstance(raw_sections, list):
        raise ToolError("'sections' must be a list.")
    order, sections = [], {}
    for raw in raw_sections:
        if not isinstance(raw, dict):
            raise ToolError("Each section must be an object with a 'type'.")
        section_id = blocks.new_section_id(str(raw.get("type") or "section"), sections)
        sections[section_id] = _normalise(raw)
        order.append(section_id)
    return order, sections


def _normalise(raw):
    out = {"type": raw.get("type"), "settings": raw.get("settings") or {}}
    if "blocks" in raw:
        out["blocks"] = raw.get("blocks") or []
    if "hidden" in raw:
        out["hidden"] = raw["hidden"]
    return out


# ------------------------------------------------------------------ read tools

def list_pages(ctx, args):
    pages = StorePage.objects.filter(store=ctx.store).order_by("-created_at")[:LIST_LIMIT]
    return {"pages": [{
        "id": str(p.pk), "title": p.title, "handle": p.handle, "status": p.status,
        "editable": p.status == StorePage.HIDDEN,
    } for p in pages]}


def get_page(ctx, args):
    page = _page(ctx, _args_str(args, "page_id"))
    data = _templates(_store_theme(ctx)).get(page.template) or {"order": [], "sections": {}}
    return {
        "id": str(page.pk), "title": page.title, "handle": page.handle, "status": page.status,
        "editable": page.status == StorePage.HIDDEN,
        "has_body_text": bool(page.body.strip()),
        "sections": [_section_view(sid, data["sections"][sid]) for sid in data["order"] if sid in data["sections"]],
    }


def describe_section_type(ctx, args):
    try:
        return blocks.describe_section_type(_args_str(args, "type"))
    except ValueError as error:
        raise ToolError(str(error)) from error


def list_collections(ctx, args):
    rows = Collection.objects.filter(store=ctx.store).order_by("name")[:LIST_LIMIT]
    return {"collections": [{"id": str(c.pk), "name": c.name, "handle": c.handle, "status": c.status} for c in rows]}


def list_products(ctx, args):
    limit = args.get("limit")
    limit = limit if isinstance(limit, int) and not isinstance(limit, bool) and 0 < limit <= LIST_LIMIT else 25
    query = Product.objects.filter(store=ctx.store).order_by("title")
    search = _args_str(args, "search", required=False, limit=100)
    if search:
        query = query.filter(title__icontains=search)
    return {"products": [{"id": str(p.pk), "title": p.title, "status": p.status} for p in query[:limit]]}


# ------------------------------------------------------------------ write tools (drafts only)

def create_page(ctx, args):
    from apps.themes.views import _unique_page_handle  # the page API's own handle rules (unique, not a policy address)
    from apps.themes.schemas import sanitize_html

    title = _args_str(args, "title")
    slug = _args_str(args, "slug", required=False)
    body = _args_str(args, "body", required=False, limit=20000)
    order, sections = _build_sections(args.get("sections"))
    if not order:
        raise ToolError("A page needs at least one section.")
    # The theme requires the Page content section (the page's title and text) on every page; sections go above it
    order.append("main")
    sections["main"] = {"type": blocks.BODY_SECTION, "settings": {}}
    template = {"order": order, "sections": sections}
    # Checked before anything is saved
    try:
        clean = blocks.validate_template("page.draft", template)
    except blocks.BlockError as error:
        raise ToolError(str(error)) from error
    _check_references(ctx, clean)

    with transaction.atomic():
        handle = _unique_page_handle(ctx.store, slugify(slug) or title)
        page = StorePage.objects.create(
            store=ctx.store, title=title, handle=handle, body=sanitize_html(body), status=StorePage.HIDDEN,
        )
        store_theme = _store_theme(ctx)
        page.template = create_template_for_page(store_theme, page)
        if not page.template:
            raise ToolError("This theme has reached its limit of page templates.", "refused")
        page.save(update_fields=["template"])
        store_theme.refresh_from_db()
        _write_template(ctx, store_theme, page.template, template)
        _ensure_draft(ctx, page, created=True)
    _record(ctx, "created_page", page, f"Created draft page /pages/{page.handle}")
    return {"page_id": str(page.pk), "handle": page.handle, "status": "draft (hidden)", "section_ids": order}


def add_section(ctx, args):
    page = _editable_page(ctx, _args_str(args, "page_id"))
    section = args.get("section")
    if not isinstance(section, dict):
        raise ToolError("'section' must be an object with a 'type'.")
    with transaction.atomic():
        _ensure_draft(ctx, page)  # first: it records the page as it was, before it gets a template of its own
        store_theme, key = _own_template(ctx, page)
        template = copy.deepcopy(_templates(store_theme)[key])
        section_id = blocks.new_section_id(str(section.get("type") or "section"), template["sections"])
        template["sections"][section_id] = _normalise(section)
        position = args.get("position")
        if position is not None and (not isinstance(position, int) or isinstance(position, bool)):
            raise ToolError("'position' must be a whole number (0 = top).")
        # New sections never go below the page's own text section unless asked to be at the very end
        order = template["order"]
        if position is None:
            body_at = [i for i, s in enumerate(order) if template["sections"][s]["type"] == blocks.BODY_SECTION]
            position = body_at[0] if body_at else len(order)
        order.insert(max(0, min(position, len(order))), section_id)
        _write_template(ctx, store_theme, key, template)
    _record(ctx, "added_section", page, f"Added a {section.get('type')} section to '{page.title}'")
    return {"section_id": section_id, "position": order.index(section_id)}


def update_section(ctx, args):
    page = _editable_page(ctx, _args_str(args, "page_id"))
    section_id = _args_str(args, "section_id")
    changes = args.get("changes")
    if not isinstance(changes, dict) or not changes:
        raise ToolError("'changes' must be an object with settings, blocks and/or hidden.")
    with transaction.atomic():
        _ensure_draft(ctx, page)  # first: it records the page as it was, before it gets a template of its own
        store_theme, key = _own_template(ctx, page)
        template = copy.deepcopy(_templates(store_theme)[key])
        current = template["sections"].get(section_id)
        if current is None:
            raise ToolError(f"No section '{section_id}' on this page. Call get_page for the real ids.", "refused")
        if current["type"] == blocks.BODY_SECTION:
            raise ToolError("That is the page's text section; change the page text instead.", "refused")
        if "settings" in changes:
            if not isinstance(changes["settings"], dict):
                raise ToolError("'settings' must be an object.")
            current["settings"] = {**current.get("settings", {}), **changes["settings"]}
        if "blocks" in changes:
            current["blocks"] = changes["blocks"] or []
        if "hidden" in changes:
            current["hidden"] = changes["hidden"]
        _write_template(ctx, store_theme, key, template)
    _record(ctx, "updated_section", page, f"Updated the {current['type']} section on '{page.title}'")
    return {"section_id": section_id, "updated": True}


def delete_section(ctx, args):
    page = _editable_page(ctx, _args_str(args, "page_id"))
    section_id = _args_str(args, "section_id")
    with transaction.atomic():
        _ensure_draft(ctx, page)  # first: it records the page as it was, before it gets a template of its own
        store_theme, key = _own_template(ctx, page)
        template = copy.deepcopy(_templates(store_theme)[key])
        current = template["sections"].get(section_id)
        if current is None:
            raise ToolError(f"No section '{section_id}' on this page. Call get_page for the real ids.", "refused")
        if current["type"] == blocks.BODY_SECTION:
            raise ToolError("The page's text section can't be removed by the assistant.", "refused")
        template["sections"].pop(section_id)
        template["order"] = [s for s in template["order"] if s != section_id]
        _write_template(ctx, store_theme, key, template)
    _record(ctx, "deleted_section", page, f"Removed a {current['type']} section from '{page.title}'")
    return {"deleted": section_id}


TOOLS = {
    f.__name__: f for f in (
        list_pages, get_page, describe_section_type, create_page, add_section, update_section, delete_section,
        list_collections, list_products,
    )
}


def run_tool(ctx, name, raw_arguments):
    """
    Run one tool call from the model. Returns (result, error_kind): error_kind is None on success, else
    "malformed" | "unknown" | "validation" | "refused", and result is {"error": message} for the model.
    """
    func = TOOLS.get(name)
    if func is None:
        return {"error": f"Unknown tool '{name}'. Available: {', '.join(TOOLS)}."}, "unknown"
    try:
        args = json.loads(raw_arguments) if isinstance(raw_arguments, str) and raw_arguments.strip() else (raw_arguments or {})
    except ValueError:
        return {"error": "The arguments were not valid JSON. Send a single JSON object."}, "malformed"
    if not isinstance(args, dict):
        return {"error": "The arguments must be a JSON object."}, "malformed"
    try:
        return func(ctx, args), None
    except ToolError as error:
        return {"error": str(error)}, error.kind
