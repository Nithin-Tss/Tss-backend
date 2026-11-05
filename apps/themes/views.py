import copy
import hashlib
import json
import re
import time
import uuid
from functools import wraps
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
from django.core import signing
from django.core.cache import cache
from django.core.files.storage import default_storage
from django.db import IntegrityError, connection, transaction
from django.db.models import F
from django.db.models.functions import Lower, Trim
from django.http import (
    Http404, HttpResponse, HttpResponsePermanentRedirect, HttpResponseRedirect, JsonResponse, StreamingHttpResponse,
)
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.http import require_GET, require_POST

from liquid.exceptions import LiquidError
from markupsafe import escape

from django.core.exceptions import DisallowedHost, ValidationError as DjangoValidationError
from rest_framework import exceptions, serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.renderers import BaseRenderer, JSONRenderer
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from apps.catalog.models import Collection
from apps.catalog.services import published_collections, published_products
from apps.core.origin import _origin_allowed
from apps.core.permissions import HasStorePermission, IsStoreMember
from apps.core.tokens import PASSWORD_CLAIM, fingerprint_matches, password_fingerprint
from django.contrib.auth import authenticate, get_user_model
from apps.customers.models import Customer, CustomerAddress
from apps.customers import services as customer_services

from apps.tenancy.models import Store, StoreMembership

from apps.media.images import public_image_url
from apps.media.models import MediaAsset
from apps.media.services import upload_image
from .models import BlogPost, Menu, MenuItem, StorePage, StoreTheme, ThemeImage, UrlRedirect
from .customizer import read_settings, save_settings, validate_settings
from . import presets
from . import privacy
import logging
from . import section_sync as sync
from .rendering import (
    _catalog, _matches_filters, _products, _with_category_ids, dead_menu_urls, parse_collection_filters, published_pages, published_posts, redirect_forms, render_cart_html,
    render_page, render_synced_section_fast, shop_url,
)
from apps.catalog.models import Product, ProductVariant
from apps.commerce import services as commerce_services
from apps.orders import services as orders_services
from apps.orders.models import Order
from . import cart as cart_data
from .rendering import _collect_fonts, google_fonts_link
from .schemas import PAGES, base_page, default_template, is_page_key, public_schema, resolve, validate_custom_data
from .services import (
    is_private,
    MAX_THEMES,
    create_theme,
    delete_path,
    delete_theme,
    duplicate_theme,
    publish_theme,
    rename_theme,
    starter_files,
    store_themes,
    generate_css_variables,
    get_store_theme,
    rename_path,
    reset_editing_theme,
    set_editing_theme,
    restore_section,
    save_file,
    save_section_values,
    synced_files,
    synced_section,
    synced_sections,
    templates_changed,
    code_files,
    create_page_template,
    create_template_for_page,
    page_template_keys,
    page_templates,
)


class StoreThemeView(APIView):
    permission_classes = [IsAuthenticated, IsStoreMember, HasStorePermission]
    permission_resource = "themes"

    def store_theme(self):
        return get_store_theme(self.request.store)

    # The admin edits the theme named by X-Theme-Id or ?theme= (a draft or the published one); without it, the
    # published theme. The public storefront never goes through here.
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        theme_id = request.headers.get("X-Theme-Id") or request.query_params.get("theme")
        self._editing = set_editing_theme(theme_id)

    def finalize_response(self, request, response, *args, **kwargs):
        token = getattr(self, "_editing", None)
        if token is not None:
            reset_editing_theme(token)
            self._editing = None
        return super().finalize_response(request, response, *args, **kwargs)


class ThemeFilesView(StoreThemeView):
    """
    GET    /api/v1/themes/files/            all files of the store's theme
    POST   /api/v1/themes/files/            create or save {path, content}
    DELETE /api/v1/themes/files/?path=...   delete a file or a folder
    """

    def get(self, request):
        theme_record = self.store_theme()
        files = code_files(theme_record)

        return Response({
            "store": str(request.store.store_id),
            "storefrontUrl": request.build_absolute_uri(
                shop_url(request.store) + "/"
            ),
            "theme": {
                "id": str(theme_record.theme.id),
                "name": theme_record.theme.name,
                "published": theme_record.is_active,
            },
            "files": [
                {
                    "path": path,
                    "content": content,
                }
                for path, content in files.items()
            ],
        })

    def post(self, request):
        saved = save_file(
            self.store_theme(),
            request.data.get("path"),
            request.data.get("content", ""),
            base_version=_int_or_none(request.data.get("base_version")),
            client=str(request.data.get("client") or "")[:64],
            started_at=_float_or_none(request.data.get("started_at")),
        )

        body = {
            "path": saved.file_path,
            "size": saved.file_size,
        }
        # A synced section (sections/footer.liquid): its new version, values and any conflict
        if getattr(saved, "sync", None):
            body["sync"] = saved.sync
        return Response(body)

    def delete(self, request):
        removed = delete_path(
            self.store_theme(),
            request.query_params.get("path"),
        )

        return Response({
            "deleted": removed,
        })


class ThemeRenameView(StoreThemeView):
    """
    POST /api/v1/themes/files/rename/
    {source, target}
    """

    def post(self, request):
        moves = rename_path(
            self.store_theme(),
            request.data.get("source"),
            request.data.get("target"),
        )

        return Response({
            "moved": moves,
        })


def _float_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _keep_sections(store_theme, clean):
    """Saving the theme's other settings never touches the synced sections: their newest saved values stay."""
    from .models import StoreTheme

    fresh = StoreTheme.objects.filter(pk=store_theme.pk).values_list("custom_data", flat=True).first() or {}
    if fresh.get("sections"):
        clean["sections"] = fresh["sections"]


def _templates_changed(store_theme, before, data):
    bases = data.get("section_versions") if isinstance(data.get("section_versions"), dict) else {}
    return templates_changed(
        store_theme, before, client=str(data.get("client") or "")[:64],
        base_versions={name: _int_or_none(bases.get(name)) for name in sync.SYNCED_TEMPLATES if name in bases},
        started_at=_float_or_none(data.get("started_at")),
    )


def _save_sections(store_theme, sections, data):
    """{"header": {"settings": {...}, "blocks": [...]}, "footer": {...}} -> each synced section saved (values + its file)."""
    if not sections:
        return {}
    bases = data.get("section_versions") if isinstance(data.get("section_versions"), dict) else {}
    client = str(data.get("client") or "")[:64]
    saved = {}
    for name, values in sections.items():
        if name not in sync.SYNCED_SECTIONS:
            raise ValidationError({"sections": f"Unknown section: {name}."})
        if not isinstance(values, dict):
            raise ValidationError({"sections": f"{name}: expected an object."})
        store_theme.refresh_from_db(fields=["custom_data"])
        saved[name] = save_section_values(
            store_theme, name, values, client=client, base_version=_int_or_none(bases.get(name)),
            started_at=_float_or_none(data.get("started_at")),
        )
    return saved


class ThemeSettingsView(StoreThemeView):
    """
    GET /api/v1/themes/settings/
        section layout and settings with defaults

    PUT /api/v1/themes/settings/
        replace them; validated against the schema
    """

    def get(self, request):
        return Response(
            resolve(self.store_theme().custom_data)
        )

    def patch(self, request):
        """
        PATCH /api/v1/themes/settings/  only what changed (the customizer autosaves this way):
        {"settings": {...}, "templates": {"index": {"order": [...], "sections": {"<id>": {...} | null}}}}
        null removes a section. The merged result is validated like a full save.
        """
        body = request.data
        if not isinstance(body, dict):
            raise ValidationError("Expected an object.")

        store_theme = self.store_theme()
        data = copy.deepcopy(store_theme.custom_data or {})
        if body.get("settings") is not None:
            if not isinstance(body["settings"], dict):
                raise ValidationError({"settings": "Expected an object."})
            data.setdefault("settings", {}).update(body["settings"])
        merged = resolve(data)

        templates = body.get("templates") or {}
        if not isinstance(templates, dict):
            raise ValidationError({"templates": "Expected an object."})

        for page, changes in templates.items():
            if not is_page_key(page) or not isinstance(changes, dict):
                raise ValidationError({"templates": f"Unknown page: {page}."})
            if page not in merged["templates"]:
                if page in PAGES:
                    merged["templates"][page] = default_template(page)
                else:
                    raise ValidationError({"templates": f"Unknown page template: {page}."})
            target = merged["templates"][page]
            for section_id, section in (changes.get("sections") or {}).items():
                if section is None:
                    target["sections"].pop(section_id, None)
                else:
                    target["sections"][section_id] = section
            if "order" in changes:
                target["order"] = changes["order"]

        sections = body.get("sections")
        if sections is not None and not isinstance(sections, dict):
            raise ValidationError({"sections": "Expected an object."})

        saved_templates = None
        if body.get("settings") is not None or templates:
            # Synced sections are saved below, by their own path (they also change their file)
            before = resolve(store_theme.custom_data)["templates"]
            merged.pop("sections", None)
            clean = validate_custom_data(merged)
            _keep_sections(store_theme, clean)
            store_theme.custom_data = clean
            store_theme.save(update_fields=["custom_data", "updated_at"])
            # The page's section lines in templates/<page>.liquid follow its new order
            saved_templates = _templates_changed(store_theme, before, request.data)

        saved_sections = _save_sections(store_theme, sections, request.data)
        reply = {"saved": True}
        if saved_sections:
            reply["sections"] = saved_sections
        if saved_templates:
            reply["templates"] = saved_templates
        return Response(reply)

    def put(self, request):
        store_theme = self.store_theme()
        data = dict(request.data) if isinstance(request.data, dict) else request.data
        sections = data.pop("sections", None) if isinstance(data, dict) else None

        clean = validate_custom_data(data)
        clean.pop("sections", None)
        _keep_sections(store_theme, clean)
        before = resolve(store_theme.custom_data)["templates"]
        store_theme.custom_data = clean

        store_theme.save(
            update_fields=[
                "custom_data",
                "updated_at",
            ]
        )

        _templates_changed(store_theme, before, request.data if isinstance(request.data, dict) else {})
        if isinstance(sections, dict):
            _save_sections(store_theme, sections, request.data)

        return Response(
            resolve(store_theme.custom_data),
            status=status.HTTP_200_OK,
        )


class ThemeLibraryView(APIView):
    """
    The store's themes: one published, the rest drafts. These calls never open a theme for editing,
    so they don't send X-Theme-Id and they don't touch any theme's files.

    GET  /api/v1/themes/library/
         {"themes": [{id, name, preset, published, updated_at}], "presets": [ready-made themes]}
    POST /api/v1/themes/library/   {"source": "<preset slug>" | "blank" | "<theme id>", "name"}
         add a theme: the store's first is published, later ones are drafts
    """

    permission_classes = [IsAuthenticated, IsStoreMember, HasStorePermission]
    permission_resource = "themes"

    def get(self, request):
        return Response({
            "themes": [_library_item(t) for t in store_themes(request.store)],
            "presets": [presets.public(p) for p in presets.list_presets()],
            "max": MAX_THEMES,
        })

    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        source = data.get("source") or "blank"
        store = request.store
        by = request.user.email
        if source == "blank":
            created = create_theme(store, data.get("name"), starter_files(), {}, created_by=by)
        elif isinstance(source, str) and re.fullmatch(r"[0-9a-fA-F-]{32,36}", source):
            created = duplicate_theme(store, source, data.get("name"), created_by=by)
        else:
            preset = presets.get_preset(source)
            files, custom_data, theme_data = presets.build(source)
            created = create_theme(store, data.get("name") or preset["name"], files, custom_data, theme_data,
                                   created_by=by)
        return Response(_library_item(created), status=status.HTTP_201_CREATED)


class ThemeLibraryItemView(APIView):
    """
    PATCH  /api/v1/themes/library/<id>/   {"name"}  rename
    DELETE /api/v1/themes/library/<id>/            delete a draft (the published theme can't be deleted)
    """

    permission_classes = [IsAuthenticated, IsStoreMember, HasStorePermission]
    permission_resource = "themes"

    def patch(self, request, theme_id):
        name = request.data.get("name") if isinstance(request.data, dict) else None
        return Response(_library_item(rename_theme(request.store, theme_id, name)))

    def delete(self, request, theme_id):
        delete_theme(request.store, theme_id)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ThemePublishView(ThemeLibraryItemView):
    """POST /api/v1/themes/library/<id>/publish/   make it the live theme; the one that was live becomes a draft"""

    http_method_names = ["post", "options"]

    def post(self, request, theme_id):
        return Response(_library_item(publish_theme(request.store, theme_id)))


def _library_item(store_theme):
    theme = store_theme.theme
    return {
        "id": str(theme.id),
        "name": theme.name,
        "preset": (theme.theme_data or {}).get("preset"),
        "published": store_theme.is_active,
        "updated_at": theme.updated_at.isoformat(),
    }


class ThemeCustomizerView(StoreThemeView):
    """
    GET  /api/v1/themes/customizer/
         current colors, fonts and button radius

    POST /api/v1/themes/customizer/
         save any subset of them into theme_data
    """

    def get(self, request):
        return Response(
            read_settings(
                self.store_theme().theme
            )
        )

    def post(self, request):
        clean, errors = validate_settings(
            request.data
        )

        if errors:
            return Response(
                errors,
                status=status.HTTP_400_BAD_REQUEST,
            )

        theme = self.store_theme().theme

        save_settings(
            theme,
            clean,
        )

        return Response(
            read_settings(theme)
        )


class ThemeImageView(StoreThemeView):
    """
    POST /api/v1/themes/images/  (multipart, field "image") -> {"url": ...}
    Banner, slide and image-block pictures: JPG, PNG or WEBP up to 1MB,
    checked by their first bytes (same rules as product photos).
    """

    parser_classes = [MultiPartParser]

    def post(self, request):
        # Kept in the database and listed in the store's media assets (product photos are not)
        asset = upload_image(request.store.store_id, request.FILES.get("image"), request.user)
        return Response({"url": public_image_url(request, asset.url)}, status=status.HTTP_201_CREATED)


def _public_image_headers(response):
    """Pictures are public and read-only (found by their random id, no cookies): any page, even a sandboxed one
    whose origin is "null", may load or read them. Only this route; the rest of the API keeps its allow-list."""
    response["Access-Control-Allow-Origin"] = "*"
    response["Cross-Origin-Resource-Policy"] = "cross-origin"
    # Never let a browser treat these bytes as anything but the picture they are, or run them if opened directly
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response


THEME_IMAGE_CACHE_SECONDS = 60 * 60
THEME_IMAGE_CACHE_MAX_BYTES = 2 * 1024 * 1024  # bigger pictures are read from the database each time


@require_GET
def theme_image(request, image_id):
    """Public: a picture (a media asset, or a product photo). Bytes are cached in Redis (a picture never changes once uploaded)."""
    key = f"theme-image:{image_id}"
    cached = cache.get(key)
    if cached is None:
        stored = MediaAsset.objects.filter(pk=image_id, storage_key__isnull=False).only("storage_key").first()
        if stored is not None:  # kept in the bucket: older /s/_img/ links keep working by redirecting there
            response = HttpResponseRedirect(stored.url)
            response["Cache-Control"] = "public, max-age=3600"
            return _public_image_headers(response)
        asset = MediaAsset.objects.filter(pk=image_id, data__isnull=False).only("data", "mime_type").first()
        if asset is not None:
            cached = (bytes(asset.data), asset.mime_type)
        else:
            image = get_object_or_404(ThemeImage, pk=image_id)
            cached = (bytes(image.data), image.content_type)
        if len(cached[0]) <= THEME_IMAGE_CACHE_MAX_BYTES:
            cache.set(key, cached, THEME_IMAGE_CACHE_SECONDS)
    data, content_type = cached
    response = HttpResponse(data, content_type=content_type)
    response["Cache-Control"] = "public, max-age=31536000, immutable"
    return _public_image_headers(response)


# Added to the customizer preview only (the Home page). The page has no access to
# the admin (sandboxed iframe), so it can only send messages.
#
# Edit mode, switched by the customizer's "Edit mode" button ({type: "mode"}):
# - Off (the default): the page is the real store. No outlines, labels or drag
#   handles; links, buttons, slideshows and forms work; clicks send nothing.
# - On: hovering a section or block outlines it (sections show their name);
#   a click selects it instead of following links or pressing buttons, and
#   tells the customizer, which opens its settings. Empty space unselects;
#   Escape unselects. Sections can be dragged to a new place by their handle.
PREVIEW_EDITOR = """
<style>
[data-tss-section]{position:relative}
.tss-edit [data-tss-section]:hover{outline:1px solid #141b2d;outline-offset:-1px}
.tss-edit [data-tss-section].tss-selected{outline:2px solid #141b2d;outline-offset:-2px}
.tss-edit [data-tss-section]:hover::before,.tss-edit [data-tss-section].tss-selected::before{
  content:attr(data-tss-label);position:absolute;top:0;left:0;z-index:51;max-width:60%;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;padding:3px 8px;border-bottom-right-radius:6px;
  background:#141b2d;color:#fff;font:600 11px/1.4 system-ui,sans-serif;pointer-events:none}
.tss-edit [data-tss-block]{position:relative}
.tss-edit [data-tss-block]:hover{outline:1px solid rgba(20,27,45,.55);outline-offset:2px}
.tss-edit [data-tss-block].tss-block-selected{outline:2px solid #141b2d;outline-offset:2px}
.tss-handle{position:absolute;top:10px;right:10px;z-index:50;display:none;align-items:center;gap:6px;
  padding:6px 10px;border:0;border-radius:8px;background:#141b2d;color:#fff;font:600 12px/1 system-ui,sans-serif;
  cursor:grab;touch-action:none;box-shadow:0 4px 12px rgba(20,27,45,.25)}
.tss-edit [data-tss-section]:hover>.tss-handle,.tss-edit .tss-selected>.tss-handle{display:inline-flex}
@media (hover:none){.tss-edit .tss-handle{display:inline-flex}}
.tss-dragging{opacity:.35}
.tss-drop-line{position:absolute;left:0;right:0;height:4px;margin-top:-2px;background:#141b2d;border-radius:4px;
  box-shadow:0 0 0 3px rgba(20,27,45,.15);z-index:60;pointer-events:none;display:none}
</style>
<script>
(function () {
  var config = __CONFIG__;
  var selected = config.selected;
  var selectedBlock = null;
  var editing = false;
  var pendingImages = {};
  function send(message) { message.source = "tss-preview"; parent.postMessage(message, "*"); }
  function all() { return [].slice.call(document.querySelectorAll("[data-tss-section]")); }
  function byId(id) { return all().filter(function (el) { return el.getAttribute("data-tss-section") === id; })[0]; }
  // The page's own sections; fixed ones (the footer) can be selected but not moved
  function movable() { return all().filter(function (el) { return !el.hasAttribute("data-tss-fixed"); }); }

  var line = document.createElement("div");
  line.className = "tss-drop-line";
  document.body.appendChild(line);

  // Outlines, labels and handles only show with the "tss-edit" class (see the styles above)
  function setMode(on) {
    editing = Boolean(on);
    document.documentElement.classList.toggle("tss-edit", editing);
  }

  function select(id, block, scroll) {
    selected = id || "";
    selectedBlock = block === undefined || block === null ? null : Number(block);
    all().forEach(function (el) { el.classList.toggle("tss-selected", el.getAttribute("data-tss-section") === selected); });
    document.querySelectorAll(".tss-block-selected").forEach(function (el) { el.classList.remove("tss-block-selected"); });
    var section = selected ? byId(selected) : null;
    if (section && selectedBlock !== null) {
      var blockEl = section.querySelector('[data-tss-block="' + selectedBlock + '"]');
      if (blockEl) blockEl.classList.add("tss-block-selected");
    }
    // Picked in the customizer's sections list: bring it into view
    if (scroll && editing && section) section.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // The store path ("/products/<id>", "/collections/all", "/blog/<handle>", "/") of a plain click on a
  // link to one of this store's pages that has a template, else null
  function storePage(e) {
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return null;
    var a = e.target.closest && e.target.closest("a[href], .cart-checkout, [data-cart-checkout-soon]");
    if (!a || (a.target && a.target !== "_self")) return null;
    if (a.matches && a.matches(".cart-checkout, [data-cart-checkout-soon]")) return "/checkout";
    var rawHref = a.getAttribute("href");
    if (!rawHref) return null;
    if (rawHref === "/checkout" || rawHref.endsWith("/checkout")) return "/checkout";
    var url;
    try { url = new URL(rawHref, document.baseURI); } catch (err) { return null; }
    if (url.origin !== new URL(document.baseURI).origin) return null;
    var m = url.pathname.match(/^\\/s\\/[^/]+(\\/.*)?$/);
    var path = m ? (m[1] || "/") : url.pathname;
    path = path.replace(/\\/+$/, "") || "/";
    if (/^\\/(products\\/[0-9a-f-]{36}|collections(\\/[^/]+)?|search|blog(\\/[^/]+){0,2}|pages\\/[^/]+|account(\\/(login|register))?|checkout(\\/.*)?|cart(\\/.*)?)?$/i.test(path)) {
      return path + (url.search || "");
    }
    return null;
  }

  // Edit mode: a click selects the section or block under it, and does nothing else -
  // except on a product or blog post link, which opens that page (as does any store link with Edit mode off).
  // The customizer opens the page's template itself, so the preview stays editable.
  document.addEventListener("click", function (e) {
    if (e.target.closest(".site-header__search-block, .site-header__search, .site-header__search-panel, .collection-search, input[type='search'], input[name='q']")) {
      return;
    }
    var page = storePage(e);
    if (page && (!editing || /^\\/(products|blog|pages|collections|search)(\\/[^/]+)?/.test(page) || /^\\/account/.test(page) || /^\\/(checkout|cart)/.test(page))) {
      e.preventDefault();
      e.stopPropagation();
      send({ type: "navigate", path: page });
      return;
    }
    if (!editing) return;
    e.preventDefault();
    e.stopPropagation();
    if (e.target.closest(".tss-handle")) return;
    var section = e.target.closest("[data-tss-section]");
    if (!section) { send({ type: "select", id: "", block: null }); return; }
    var blockEl = e.target.closest("[data-tss-block]");
    var block = blockEl && section.contains(blockEl) ? Number(blockEl.getAttribute("data-tss-block")) : null;
    send({ type: "select", id: section.getAttribute("data-tss-section"), block: block });
  }, true);

  document.addEventListener("submit", function (e) {
    var form = e.target.closest("form[role='search'], .site-header__search-panel, .collection-search");
    if (form) {
      var input = form.querySelector("input[name='q'], input[type='search']");
      var q = input ? input.value.trim() : "";
      e.preventDefault();
      e.stopPropagation();
      var action = form.getAttribute("action") || "/collections/all";
      var cleanPath = (action.replace(/^https?:\/\/[^/]+/, "").replace(/^\/s\/[^/]+/, "") || "/collections/all").split("?")[0];
      var path = q ? cleanPath + "?q=" + encodeURIComponent(q) : cleanPath;
      send({ type: "navigate", path: path });
    }
  }, true);
  document.addEventListener("contextmenu", function (e) {
    if (!editing) return;
    e.preventDefault();
    e.stopPropagation();
    var section = e.target.closest("[data-tss-section]");
    if (!section) { send({ type: "select", id: "", block: null }); return; }
    var blockEl = e.target.closest("[data-tss-block]");
    var block = blockEl && section.contains(blockEl) ? Number(blockEl.getAttribute("data-tss-block")) : null;
    send({ type: "context", id: section.getAttribute("data-tss-section"), block: block, x: e.clientX, y: e.clientY });
  }, true);
  document.addEventListener("submit", function (e) { if (editing) e.preventDefault(); }, true);
  document.addEventListener("keydown", function (e) {
    if (editing && e.key === "Escape") send({ type: "escape" });
  });

  // Drag handle (Edit mode only: the handle is hidden otherwise)
  function enhance(el) {
    if (el.__tssReady || el.hasAttribute("data-tss-fixed")) return;
    el.__tssReady = true;
    var id = el.getAttribute("data-tss-section");
    var handle = document.createElement("button");
    handle.type = "button";
    handle.className = "tss-handle";
    handle.setAttribute("aria-label", "Drag to move this section");
    handle.innerHTML = "&#10303; Move";
    el.appendChild(handle);
    handle.addEventListener("pointerdown", function (e) { if (editing) startDrag(e, el, id); });
  }

  function startDrag(e, el, id) {
    e.preventDefault();
    var target = null;
    el.classList.add("tss-dragging");

    function move(ev) {
      target = null;
      var y = ev.clientY;
      var sections = movable();
      for (var i = 0; i < sections.length; i++) {
        var other = sections[i];
        if (other === el) continue;
        var box = other.getBoundingClientRect();
        if (y >= box.top && y <= box.bottom) {
          var after = y > box.top + box.height / 2;
          target = { id: other.getAttribute("data-tss-section"), after: after };
          line.style.top = (after ? box.bottom : box.top) + window.scrollY + "px";
          line.style.display = "block";
          break;
        }
      }
      if (!target) line.style.display = "none";
      if (y < 60) window.scrollBy(0, -12);
      else if (y > window.innerHeight - 60) window.scrollBy(0, 12);
    }

    function end() {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", end);
      window.removeEventListener("pointercancel", end);
      el.classList.remove("tss-dragging");
      line.style.display = "none";
      if (target) send({ type: "move-section", id: id, target: target.id, after: target.after });
    }

    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", end);
    window.addEventListener("pointercancel", end);
  }

  // ---- small updates from the customizer (no reload) ----

  function swapPending(root) {
    Object.keys(pendingImages).forEach(function (url) {
      root.querySelectorAll('img[src="' + url + '"],video[poster="' + url + '"]').forEach(function (node) {
        node.setAttribute(node.tagName === "VIDEO" ? "poster" : "src", pendingImages[url]);
      });
      root.querySelectorAll('source[srcset="' + url + '"]').forEach(function (node) { node.setAttribute("srcset", pendingImages[url]); });
    });
  }

  // Wait (briefly) for new images so swapping a section never flashes empty
  function preload(root, done) {
    var imgs = [].slice.call(root.querySelectorAll("img[src]"));
    var left = imgs.length, finished = false;
    function finish() { if (!finished) { finished = true; done(); } }
    if (!left) return finish();
    imgs.forEach(function (img) {
      var probe = new Image();
      probe.onload = probe.onerror = function () { if (--left <= 0) finish(); };
      probe.src = img.getAttribute("src");
    });
    setTimeout(finish, 700);
  }

  function runScripts(root) {
    root.querySelectorAll("script").forEach(function (old) {
      var fresh = document.createElement("script");
      fresh.textContent = old.textContent;
      old.replaceWith(fresh);
    });
  }

  function place(el, index) {
    var others = movable().filter(function (x) { return x !== el; });
    if (!others.length) return false;
    if (index >= others.length) others[others.length - 1].after(el);
    else others[Math.max(0, index)].before(el);
    return true;
  }

  function patch(message) {
    var tpl = document.createElement("template");
    tpl.innerHTML = message.html;
    var fresh = tpl.content.querySelector("[data-tss-section]");
    var old = byId(message.id);
    if (!fresh) { if (old) old.remove(); return; }
    swapPending(fresh);
    fresh.querySelectorAll(".tss-anim-fade,.tss-anim-slide-up").forEach(function (x) { x.classList.add("tss-in"); });
    preload(fresh, function () {
      var current = byId(message.id);
      if (current) current.replaceWith(fresh);
      else if (!place(fresh, message.index)) { send({ type: "need-reload" }); return; }
      runScripts(fresh);
      enhance(fresh);
      select(selected, selectedBlock);
      // The customizer measures "change made -> preview updated" with this
      if (message.token) send({ type: "patched", id: message.id, token: message.token });
    });
  }

  function reorder(ids) {
    var first = movable()[0];
    if (!first) return;
    var marker = document.createComment("tss");
    first.before(marker);
    ids.forEach(function (id) { var el = byId(id); if (el) marker.before(el); });
    marker.remove();
  }

  function addFonts(html) {
    var tpl = document.createElement("template");
    tpl.innerHTML = html || "";
    tpl.content.querySelectorAll("link[rel=stylesheet]").forEach(function (link) {
      var href = link.getAttribute("href");
      var exists = [].some.call(document.querySelectorAll("link[rel=stylesheet]"), function (l) { return l.getAttribute("href") === href; });
      if (!exists) document.head.appendChild(link);
    });
  }

  window.addEventListener("message", function (e) {
    if (e.source !== parent) return;
    var m = e.data;
    if (!m || m.source !== "tss-editor") return;
    if (m.type === "patch") patch(m);
    else if (m.type === "remove") { var el = byId(m.id); if (el) el.remove(); }
    else if (m.type === "order") reorder(m.ids || []);
    // A reorder or removal the customizer timed: "change made -> preview updated"
    if ((m.type === "remove" || m.type === "order") && m.token) send({ type: "patched", id: m.id || "", token: m.token });
    else if (m.type === "select") select(m.id, m.block, m.scroll);
    else if (m.type === "mode") setMode(m.edit);
    else if (m.type === "fonts") addFonts(m.html);
    else if (m.type === "pending-image") { pendingImages[m.url] = m.dataUrl; swapPending(document); }
  });

  all().forEach(enhance);
  select(selected);

  // Keep the scroll position when the preview fully reloads
  if (config.scroll) window.scrollTo(0, config.scroll);
  var timer = null;
  window.addEventListener("scroll", function () {
    clearTimeout(timer);
    timer = setTimeout(function () { send({ type: "scroll", y: Math.round(window.scrollY) }); }, 80);
  }, { passive: true });

  // Edit mode off, a link opened another store page inside the preview: the customizer is told
  window.addEventListener("pagehide", function () { send({ type: "leaving" }); });

  send({ type: "ready" });
})();
</script>
"""


class ThemeSectionsView(StoreThemeView):
    """
    GET /api/v1/themes/sections/
        what syncs with the customizer (code <-> customizer):
        {"header": {kind: "section", spec, values, version, path, label},
         "index":  {kind: "template", page, template: {order, sections}, version, path, label}}
    """

    def get(self, request):
        return Response(synced_files(self.store_theme()))


class SectionHistoryView(StoreThemeView):
    """
    GET  /api/v1/themes/sections/<name>/history/    the last 20 versions of its file
    POST /api/v1/themes/sections/<name>/history/    {index} puts that version back
    """

    def _name(self, name):
        if name not in sync.SYNCED_FILES:
            raise Http404
        return name

    def get(self, request, name):
        store_theme = self.store_theme()
        self._name(name)
        return Response({
            "version": sync.section_state(store_theme.theme, name)["version"],
            "history": sync.history_of(store_theme.theme, name),
        })

    def post(self, request, name):
        index = _int_or_none(request.data.get("index"))
        if index is None:
            raise ValidationError({"index": "Which version to restore."})
        saved = restore_section(
            self.store_theme(), self._name(name), index, client=str(request.data.get("client") or "")[:64],
        )
        return Response({"path": saved.file_path, "size": saved.file_size, "sync": saved.sync})


class EventStreamRenderer(BaseRenderer):
    media_type = "text/event-stream"
    format = "event-stream"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        return json.dumps(data).encode("utf-8")


class ThemeEventsView(StoreThemeView):
    """
    GET /api/v1/themes/events/?after=<seq>   Server-Sent Events for this store
        "section" events when a synced section changes (from the customizer, the code editor or a restore).
    Each connection stays open about a minute; the client reconnects with ?after=<last id>.
    """

    renderer_classes = [JSONRenderer, EventStreamRenderer]
    STREAM_SECONDS = 55
    PING_SECONDS = 15

    def get(self, request):
        store_id = request.store.pk
        after = _int_or_none(request.query_params.get("after"))
        if after is None:
            after = sync.EVENTS.last_seq
        # The stream holds no database connection while it waits
        if not connection.in_atomic_block:
            connection.close()

        def stream():
            last = after
            yield f"retry: 1000\nevent: hello\ndata: {json.dumps({'seq': last})}\n\n"
            ends = time.monotonic() + self.STREAM_SECONDS
            while time.monotonic() < ends:
                events, last = sync.EVENTS.wait(store_id, last, min(self.PING_SECONDS, max(ends - time.monotonic(), 0.1)))
                if not events:
                    yield ": ping\n\n"
                    continue
                for seq, payload in events:
                    yield f"id: {seq}\nevent: {payload.get('type', 'message')}\ndata: {json.dumps(payload)}\n\n"

        response = StreamingHttpResponse(stream(), content_type="text/event-stream")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response


class SyncTimingsView(StoreThemeView):
    """
    GET  /api/v1/themes/sync-timings/   average and slowest "change made -> preview updated" times
    POST /api/v1/themes/sync-timings/   {kind, ms} reported by the customizer / code editor
    """

    action_permissions = {"POST": "themes.view"}
    # "<section>: <step>" for every synced section, e.g. "header: sidebar -> preview":
    #   sidebar -> preview     customizer: an edit, until the preview shows it
    #   code save -> preview   Save in Edit code, until the customizer's preview shows it
    #   sidebar -> code        customizer: an edit, until Edit code shows the new file
    #   code save -> blocks    Save in Edit code, until the response (sidebar rebuilt)
    KINDS = sync.TIMING_KINDS

    def get(self, request):
        return Response(sync.TIMINGS.summary())

    def post(self, request):
        kind = request.data.get("kind")
        try:
            ms = float(request.data.get("ms"))
        except (TypeError, ValueError):
            ms = -1
        if kind not in self.KINDS or not 0 <= ms < 600000:
            raise ValidationError("Expected {kind, ms}.")
        sync.TIMINGS.add(kind, ms)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------- blog posts


def _unique_post_handle(store, title, exclude_pk=None):
    """slugify(title), with -2, -3, ... added while another post of the store uses it."""
    base = slugify(title or "")[:200].strip("-") or "post"
    used = set(BlogPost.objects.filter(store=store, handle__startswith=base)
               .exclude(pk=exclude_pk).values_list("handle", flat=True))
    handle, n = base, 2
    while handle in used:
        handle, n = f"{base}-{n}", n + 1
    return handle


class BlogPostSerializer(serializers.ModelSerializer):
    """A blog post as the admin edits it. handle: typed, or made from the title; unique in the store."""

    title = serializers.CharField(max_length=255)
    handle = serializers.CharField(max_length=255, required=False, allow_blank=True)
    body = serializers.CharField(required=False, allow_blank=True, max_length=200000)
    excerpt = serializers.CharField(required=False, allow_blank=True, max_length=1000)
    cover_image = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    tags = serializers.ListField(child=serializers.CharField(max_length=60), required=False, max_length=20)
    url = serializers.SerializerMethodField()
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)

    class Meta:
        model = BlogPost
        fields = [
            "id", "title", "handle", "excerpt", "body", "cover_image", "author", "category", "tags",
            "status", "published_at", "url", "createdAt", "updatedAt",
        ]
        read_only_fields = ["published_at"]

    def get_url(self, post):
        request = self.context["request"]
        return request.build_absolute_uri(f"{shop_url(post.store)}/blog/{post.handle}")

    def validate_title(self, value):
        if not value.strip():
            raise serializers.ValidationError("Title is required.")
        return value.strip()

    def validate_handle(self, value):
        return slugify(value)[:255].strip("-")

    def validate_cover_image(self, value):
        value = value.strip()
        if value and not re.match(r"^(https?://|/(?!/))", value):
            raise serializers.ValidationError("Use an uploaded image or a link starting with https://.")
        return value

    def validate_tags(self, value):
        return list(dict.fromkeys(t.strip() for t in value if t.strip()))

    def validate(self, attrs):
        store = self.context["request"].store
        current = self.instance
        if attrs.get("handle"):
            taken = BlogPost.objects.filter(store=store, handle=attrs["handle"])
            if current is not None:
                taken = taken.exclude(pk=current.pk)
            if taken.exists():
                raise serializers.ValidationError({"handle": "Another post already uses this URL handle."})
        elif "handle" in attrs or current is None:
            title = attrs.get("title") or (current.title if current else "")
            attrs["handle"] = _unique_post_handle(store, title, exclude_pk=current.pk if current else None)
        # Published for the first time: that is the post's date
        published = attrs.get("status", current.status if current else BlogPost.DRAFT) == BlogPost.PUBLISHED
        if published and not (current and current.published_at):
            attrs["published_at"] = timezone.now()
        return attrs

    def create(self, validated_data):
        return BlogPost.objects.create(store=self.context["request"].store, **validated_data)


class BlogPostsView(StoreThemeView):
    """
    GET  /api/v1/themes/posts/   the store's posts, newest first (drafts too)
    POST /api/v1/themes/posts/   create {title, handle?, excerpt, body, cover_image, author, category, tags, status}
    """

    def get(self, request):
        posts = BlogPost.objects.filter(store=request.store).select_related("store").order_by(
            F("published_at").desc(nulls_first=True), "-created_at"
        )
        return Response(BlogPostSerializer(posts, many=True, context={"request": request}).data)

    def post(self, request):
        serializer = BlogPostSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class BlogPostView(StoreThemeView):
    """GET / PATCH / DELETE /api/v1/themes/posts/<id>/"""

    def get_post(self, post_id):
        return get_object_or_404(BlogPost.objects.select_related("store"), store=self.request.store, pk=post_id)

    def get(self, request, post_id):
        return Response(BlogPostSerializer(self.get_post(post_id), context={"request": request}).data)

    def patch(self, request, post_id):
        serializer = BlogPostSerializer(self.get_post(post_id), data=request.data, partial=True, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, post_id):
        self.get_post(post_id).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------- pages


def _unique_page_handle(store, title, exclude_pk=None):
    """slugify(title), with -2, -3, ... added while another page of the store uses it."""
    base = slugify(title or "")[:200].strip("-") or "page"
    used = set(StorePage.objects.filter(store=store, handle__startswith=base)
               .exclude(pk=exclude_pk).values_list("handle", flat=True))
    handle, n = base, 2
    while handle in used:
        handle, n = f"{base}-{n}", n + 1
    return handle


class StorePageSerializer(serializers.ModelSerializer):
    """A page as the admin edits it. handle: typed, or made from the title; unique in the store."""

    title = serializers.CharField(max_length=255)
    handle = serializers.CharField(max_length=255, required=False, allow_blank=True)
    body = serializers.CharField(required=False, allow_blank=True, max_length=200000)
    seo_title = serializers.CharField(required=False, allow_blank=True, max_length=255)
    seo_description = serializers.CharField(required=False, allow_blank=True, max_length=1000)
    template = serializers.CharField(required=False, allow_blank=True, max_length=60)
    url = serializers.SerializerMethodField()
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)

    class Meta:
        model = StorePage
        fields = [
            "id", "title", "handle", "body", "status", "template", "seo_title", "seo_description",
            "published_at", "url", "createdAt", "updatedAt",
        ]
        read_only_fields = ["published_at"]

    def get_url(self, page):
        request = self.context["request"]
        return request.build_absolute_uri(f"{shop_url(page.store)}/pages/{page.handle}")

    def validate_template(self, value):
        """Empty (the base Page template) or a page template the store has."""
        value = (value or "").strip()
        if value in ("", "page"):
            return ""
        if value not in page_template_keys(get_store_theme(self.context["request"].store).custom_data):
            raise serializers.ValidationError("Choose one of the store's page templates.")
        return value

    def validate_title(self, value):
        if not value.strip():
            raise serializers.ValidationError("Title is required.")
        return value.strip()

    def validate_handle(self, value):
        return slugify(value)[:255].strip("-")

    def validate(self, attrs):
        store = self.context["request"].store
        current = self.instance
        if attrs.get("handle"):
            taken = StorePage.objects.filter(store=store, handle=attrs["handle"])
            if current is not None:
                taken = taken.exclude(pk=current.pk)
            if taken.exists():
                raise serializers.ValidationError({"handle": "Another page already uses this URL handle."})
        elif "handle" in attrs or current is None:
            title = attrs.get("title") or (current.title if current else "")
            attrs["handle"] = _unique_page_handle(store, title, exclude_pk=current.pk if current else None)
        # Visible for the first time: that is the page's publish date
        visible = attrs.get("status", current.status if current else StorePage.VISIBLE) == StorePage.VISIBLE
        if visible and not (current and current.published_at):
            attrs["published_at"] = timezone.now()
        return attrs

    def create(self, validated_data):
        return StorePage.objects.create(store=self.context["request"].store, **validated_data)


class StorePagesView(StoreThemeView):
    """
    GET  /api/v1/themes/pages/   the store's pages, newest first (hidden ones too)
    POST /api/v1/themes/pages/   create {title, handle?, body, status, seo_title, seo_description}
    """

    def get(self, request):
        pages = StorePage.objects.filter(store=request.store).select_related("store").order_by("-created_at")
        return Response(StorePageSerializer(pages, many=True, context={"request": request}).data)

    def post(self, request):
        serializer = StorePageSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        page = serializer.save()
        if not page.template:
            # Every new page gets its own template in the code editor: templates/page.<handle>.liquid
            page.template = create_template_for_page(self.store_theme(), page)
            page.save(update_fields=["template"])
        return Response(StorePageSerializer(page, context={"request": request}).data, status=status.HTTP_201_CREATED)


class StorePageView(StoreThemeView):
    """GET / PATCH / DELETE /api/v1/themes/pages/<id>/"""

    def get_page(self, page_id):
        return get_object_or_404(StorePage.objects.select_related("store"), store=self.request.store, pk=page_id)

    def get(self, request, page_id):
        return Response(StorePageSerializer(self.get_page(page_id), context={"request": request}).data)

    def patch(self, request, page_id):
        serializer = StorePageSerializer(self.get_page(page_id), data=request.data, partial=True, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, page_id):
        self.get_page(page_id).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------- menus & redirects

class MenuItemSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(read_only=True)
    title = serializers.CharField(max_length=255)
    url = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")
    order = serializers.IntegerField(default=0)
    parent = serializers.PrimaryKeyRelatedField(queryset=MenuItem.objects.all(), required=False, allow_null=True)
    is_mega = serializers.BooleanField(required=False, default=False)
    badge = serializers.CharField(max_length=30, required=False, allow_blank=True, default="")
    promo_image = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")
    promo_title = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    promo_subtitle = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    promo_url = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")
    children = serializers.SerializerMethodField()
    broken = serializers.SerializerMethodField()

    class Meta:
        model = MenuItem
        fields = [
            "id", "title", "url", "order", "parent",
            "is_mega", "badge", "promo_image", "promo_title", "promo_subtitle", "promo_url",
            "children", "broken",
        ]

    def get_broken(self, obj):
        """True when the link goes to a product or collection that shoppers can't open now (see dead_menu_urls)."""
        return obj.url in self.context.get("broken_urls", ())

    def get_children(self, obj):
        depth = self.context.get("_depth", None)
        if depth is None:
            depth = 1
            curr = obj
            while curr.parent_id is not None:
                depth += 1
                curr = curr.parent
                if depth >= 3:
                    break
        if depth >= 3:
            return []
        child_context = {**self.context, "_depth": depth + 1}
        children = sorted(obj.children.all(), key=lambda c: (c.order, c.created_at))
        return MenuItemSerializer(children, many=True, context=child_context).data


MAX_MENU_ITEMS = 100


def _clean_menu_link(raw, field):
    """One {title, url, order?, is_mega?, badge?, promo_image?, promo_title?, promo_subtitle?, promo_url?} entry as stored; None when it has no title (the admin drops blank rows)."""
    if not isinstance(raw, dict):
        raise ValidationError({field: "Each menu item must be an object."})
    title = str(raw.get("title") or "").strip()
    if not title:
        return None
    url = str(raw.get("url") or "").strip()
    if url and not sync._SAFE_URL.match(url):
        # javascript:, data: and other schemes never reach the storefront
        raise ValidationError({field: f'Link "{title}" must start with /, #, http(s)://, mailto: or tel:.'})
    if len(title) > 255 or len(url) > 500:
        raise ValidationError({field: f'Link "{title[:40]}" is too long.'})

    badge = str(raw.get("badge") or "").strip()
    if len(badge) > 30:
        raise ValidationError({field: f'Badge "{badge[:20]}" is too long (max 30 characters).'})

    promo_image = str(raw.get("promo_image") or "").strip()
    if promo_image:
        if not sync._SAFE_URL.match(promo_image):
            raise ValidationError({field: 'Promo image URL must start with /, #, or http(s)://.'})
        if len(promo_image) > 1000:
            raise ValidationError({field: "Promo image link is too long."})

    promo_title = str(raw.get("promo_title") or "").strip()
    if len(promo_title) > 255:
        raise ValidationError({field: "Promo title is too long."})

    promo_subtitle = str(raw.get("promo_subtitle") or "").strip()
    if len(promo_subtitle) > 255:
        raise ValidationError({field: "Promo subtitle is too long."})

    promo_url = str(raw.get("promo_url") or "").strip()
    if promo_url:
        if not sync._SAFE_URL.match(promo_url):
            raise ValidationError({field: 'Promo destination link must start with /, #, http(s)://, mailto: or tel:.'})
        if len(promo_url) > 500:
            raise ValidationError({field: "Promo link is too long."})

    return {
        "title": title,
        "url": url,
        "order": raw.get("order"),
        "is_mega": bool(raw.get("is_mega")),
        "badge": badge,
        "promo_image": promo_image,
        "promo_title": promo_title,
        "promo_subtitle": promo_subtitle,
        "promo_url": promo_url,
    }


def _clean_menu_items(items_data):
    """Validated [{title, url, order, children: [...]}] from the request; menus nest up to three levels deep."""
    if items_data is None:
        return []
    if not isinstance(items_data, list):
        raise ValidationError({"items": "Items must be a list."})
    cleaned, total = [], 0
    for item in items_data:
        link = _clean_menu_link(item, "items")
        if link is None:
            continue
        children = item.get("children") or []
        if not isinstance(children, list):
            raise ValidationError({"items": "Children must be a list."})

        cleaned_children = []
        for child in children:
            child_link = _clean_menu_link(child, "items")
            if child_link is None:
                continue
            subchildren = child.get("children") or []
            if not isinstance(subchildren, list):
                raise ValidationError({"items": "Children must be a list."})

            cleaned_subchildren = []
            for subchild in subchildren:
                sub_link = _clean_menu_link(subchild, "items")
                if sub_link is None:
                    continue
                level4 = subchild.get("children")
                if level4 is not None:
                    if not isinstance(level4, list):
                        raise ValidationError({"items": "Children must be a list."})
                    if len(level4) > 0:
                        raise ValidationError({"items": "Menu items cannot be nested more than 3 levels deep."})
                cleaned_subchildren.append(sub_link)

            child_link["children"] = cleaned_subchildren
            total += len(cleaned_subchildren)
            cleaned_children.append(child_link)

        link["children"] = cleaned_children
        total += 1 + len(cleaned_children)
        cleaned.append(link)

    if total > MAX_MENU_ITEMS:
        raise ValidationError({"items": f"A menu can have at most {MAX_MENU_ITEMS} items."})
    return cleaned


def _order(value, fallback):
    return value if isinstance(value, int) and value >= 0 else fallback


def _available_menu_handle(store, name, exclude_pk=None):
    base = slugify(name)[:255]
    if not base:
        return ""

    menus = Menu.objects.filter(store=store)
    if exclude_pk is not None:
        menus = menus.exclude(pk=exclude_pk)

    handle = base
    suffix = 2
    while menus.filter(handle=handle).exists():
        suffix_text = f"-{suffix}"
        handle = f"{base[:255 - len(suffix_text)].rstrip('-')}{suffix_text}"
        suffix += 1
    return handle


def _menu_uniqueness_conflict_field(error):
    cause = error.__cause__ or error
    constraint_name = getattr(getattr(cause, "diag", None), "constraint_name", None)
    if constraint_name == "uq_menu_store_name_ci":
        return "name"
    if constraint_name == "uq_menu_store_handle":
        return "handle"

    message = str(cause)
    if "uq_menu_store_name_ci" in message:
        return "name"
    if (
        "themes__menus.store_id" in message
        and "themes__menus.handle" in message
        and "UNIQUE constraint failed" in message
    ):
        return "handle"
    return None


def _sync_menu_items(menu, items):
    """Replaces the menu's items with the output of _clean_menu_items."""
    with transaction.atomic():
        menu.items.all().delete()
        for idx, item in enumerate(items):
            parent = MenuItem.objects.create(
                menu=menu,
                title=item["title"],
                url=item["url"],
                order=_order(item["order"], idx),
                is_mega=item.get("is_mega", False),
                badge=item.get("badge", ""),
                promo_image=item.get("promo_image", ""),
                promo_title=item.get("promo_title", ""),
                promo_subtitle=item.get("promo_subtitle", ""),
                promo_url=item.get("promo_url", ""),
            )
            for c_idx, child in enumerate(item.get("children", [])):
                col = MenuItem.objects.create(
                    menu=menu,
                    parent=parent,
                    title=child["title"],
                    url=child["url"],
                    order=_order(child["order"], c_idx),
                    is_mega=child.get("is_mega", False),
                    badge=child.get("badge", ""),
                    promo_image=child.get("promo_image", ""),
                    promo_title=child.get("promo_title", ""),
                    promo_subtitle=child.get("promo_subtitle", ""),
                    promo_url=child.get("promo_url", ""),
                )
                subchildren = child.get("children", [])
                if subchildren:
                    MenuItem.objects.bulk_create([
                        MenuItem(
                            menu=menu,
                            parent=col,
                            title=s["title"],
                            url=s["url"],
                            order=_order(s["order"], s_idx),
                            is_mega=s.get("is_mega", False),
                            badge=s.get("badge", ""),
                            promo_image=s.get("promo_image", ""),
                            promo_title=s.get("promo_title", ""),
                            promo_subtitle=s.get("promo_subtitle", ""),
                            promo_url=s.get("promo_url", ""),
                        )
                        for s_idx, s in enumerate(subchildren)
                    ])


class MenuSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(max_length=255)
    handle = serializers.SlugField(max_length=255, required=False, allow_blank=True, default="")
    items = serializers.SerializerMethodField()
    items_summary = serializers.SerializerMethodField()
    items_count = serializers.SerializerMethodField()

    class Meta:
        model = Menu
        fields = ["id", "name", "handle", "items", "items_summary", "items_count", "created_at", "updated_at"]

    def _top_items(self, obj):
        return sorted((i for i in obj.items.all() if i.parent_id is None), key=lambda i: (i.order, i.created_at))

    def get_items(self, obj):
        all_items = list(obj.items.all())
        by_parent = {}
        for item in all_items:
            by_parent.setdefault(item.parent_id, []).append(item)
        for item in all_items:
            if not hasattr(item, "_prefetched_objects_cache"):
                item._prefetched_objects_cache = {}
            item._prefetched_objects_cache["children"] = sorted(
                by_parent.get(item.id, []), key=lambda c: (c.order, c.created_at)
            )
        return MenuItemSerializer(self._top_items(obj), many=True, context=self.context).data

    def get_items_summary(self, obj):
        return ", ".join(i.title for i in self._top_items(obj))

    def get_items_count(self, obj):
        return len(obj.items.all())

    def validate_name(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError("Menu name is required.")
        return value.strip()

    def validate(self, attrs):
        store = self.context["request"].store
        others = Menu.objects.filter(store=store)
        if self.instance:
            others = others.exclude(pk=self.instance.pk)
        name = attrs.get("name")
        if name:
            duplicate_name = others.annotate(
                _normalized_name=Lower(Trim("name", characters=" \t\n\r\f\v"))
            ).filter(_normalized_name=name.strip().lower()).exists()
            if duplicate_name:
                raise serializers.ValidationError({"name": "A menu with this name already exists."})

        handle = attrs.get("handle")
        explicit_handle = "handle" in self.initial_data and bool(handle)
        if explicit_handle and others.filter(handle=handle).exists():
            raise serializers.ValidationError({"handle": "A menu with this handle already exists."})
        if not explicit_handle and (self.instance is None or name):
            self._generated_handle = _available_menu_handle(store, name or self.instance.name, self.instance.pk if self.instance else None)

        if "items" in self.context["request"].data:
            self._items = _clean_menu_items(self.context["request"].data.get("items"))
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        name = validated_data["name"].strip()
        handle = validated_data.get("handle") or getattr(self, "_generated_handle", "")
        try:
            menu = Menu.objects.create(store=self.context["request"].store, name=name, handle=handle)
        except IntegrityError as error:
            field = _menu_uniqueness_conflict_field(error)
            if field is None:
                raise
            raise serializers.ValidationError(
                {field: f"A menu with this {field} already exists."}
            ) from error
        _sync_menu_items(menu, getattr(self, "_items", []))
        return menu

    @transaction.atomic
    def update(self, instance, validated_data):
        instance.name = validated_data.get("name", instance.name).strip()
        if validated_data.get("handle"):
            instance.handle = validated_data["handle"]
        elif "name" in validated_data:
            instance.handle = getattr(self, "_generated_handle", "")
        try:
            instance.save()
        except IntegrityError as error:
            field = _menu_uniqueness_conflict_field(error)
            if field is None:
                raise
            raise serializers.ValidationError(
                {field: f"A menu with this {field} already exists."}
            ) from error
        if hasattr(self, "_items"):
            _sync_menu_items(instance, self._items)
        return instance


def _menu_context(request, menus):
    """What the menu serializers need to mark broken links: every link of these menus checked together."""
    urls = [item.url for menu in menus for item in menu.items.all() if item.url]
    return {"request": request, "broken_urls": dead_menu_urls(request.store, urls)}


def _menu_data(request, menu):
    """One menu as the admin sees it, read fresh after a save, with its broken links marked."""
    menu = Menu.objects.prefetch_related("items__children").get(pk=menu.pk)
    return MenuSerializer(menu, context=_menu_context(request, [menu])).data


class MenusView(StoreThemeView):
    """
    GET  /api/v1/themes/menus/    list all menus for store
    POST /api/v1/themes/menus/    create menu { name, handle?, items?: [...] }
    """

    def get(self, request):
        menus = list(Menu.objects.filter(store=request.store).prefetch_related("items__children").order_by("created_at"))
        return Response(MenuSerializer(menus, many=True, context=_menu_context(request, menus)).data)

    def post(self, request):
        serializer = MenuSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(_menu_data(request, serializer.instance), status=status.HTTP_201_CREATED)


class MenuView(StoreThemeView):
    """GET / PUT / PATCH / DELETE /api/v1/themes/menus/<id>/"""

    def get_menu(self, menu_id):
        return get_object_or_404(
            Menu.objects.prefetch_related("items__children"), store=self.request.store, pk=menu_id
        )

    def get(self, request, menu_id):
        return Response(_menu_data(request, self.get_menu(menu_id)))

    def put(self, request, menu_id):
        serializer = MenuSerializer(self.get_menu(menu_id), data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(_menu_data(request, serializer.instance))

    def patch(self, request, menu_id):
        serializer = MenuSerializer(
            self.get_menu(menu_id), data=request.data, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(_menu_data(request, serializer.instance))

    def delete(self, request, menu_id):
        self.get_menu(menu_id).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class UrlRedirectSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(read_only=True)
    path = serializers.CharField(max_length=500)
    target = serializers.CharField(max_length=500)

    class Meta:
        model = UrlRedirect
        fields = ["id", "path", "target", "created_at", "updated_at"]

    def validate_path(self, value):
        val = value.strip()
        if not val:
            raise serializers.ValidationError("Redirect from path is required.")
        if not val.startswith("/"):
            val = "/" + val
        return val

    def validate_target(self, value):
        val = value.strip()
        if not val:
            raise serializers.ValidationError("Redirect to target is required.")
        if not (val.startswith("/") or val.startswith("http://") or val.startswith("https://")):
            val = "/" + val
        return val

    def validate(self, attrs):
        store = self.context["request"].store
        path = attrs.get("path")
        target = attrs.get("target")
        if path and target and path.rstrip("/") == target.rstrip("/"):
            raise serializers.ValidationError(
                {"target": "Destination path cannot be identical to the redirect from path."}
            )
        if path:
            qs = UrlRedirect.objects.filter(store=store, path=path)
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError({"path": "A redirect for this path already exists."})
        return attrs

    def create(self, validated_data):
        return UrlRedirect.objects.create(store=self.context["request"].store, **validated_data)


class UrlRedirectsView(StoreThemeView):
    """
    GET  /api/v1/themes/redirects/    list all URL redirects
    POST /api/v1/themes/redirects/    create URL redirect { path, target }
    """

    def get(self, request):
        redirects = UrlRedirect.objects.filter(store=request.store).order_by("-created_at")
        return Response(UrlRedirectSerializer(redirects, many=True, context={"request": request}).data)

    def post(self, request):
        serializer = UrlRedirectSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class UrlRedirectView(StoreThemeView):
    """GET / PATCH / DELETE /api/v1/themes/redirects/<id>/"""

    def get_redirect(self, redirect_id):
        return get_object_or_404(UrlRedirect, store=self.request.store, pk=redirect_id)

    def get(self, request, redirect_id):
        return Response(
            UrlRedirectSerializer(self.get_redirect(redirect_id), context={"request": request}).data
        )

    def patch(self, request, redirect_id):
        serializer = UrlRedirectSerializer(
            self.get_redirect(redirect_id), data=request.data, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, redirect_id):
        self.get_redirect(redirect_id).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PageTemplatesView(StoreThemeView):
    """
    GET    /api/v1/themes/page-templates/              Page and its alternate templates: [{key, label, path, liquid_path}]
    POST   /api/v1/themes/page-templates/  {name}      a new template (a copy of Page): templates/page.<name>.liquid + .json
    DELETE /api/v1/themes/page-templates/?key=page.x   delete it (its pages go back to Page)
    """

    def get(self, request):
        return Response({"templates": page_templates(self.store_theme())})

    def post(self, request):
        store_theme = self.store_theme()
        key = create_page_template(store_theme, request.data.get("name"))
        return Response(
            {"key": key, "templates": page_templates(store_theme)}, status=status.HTTP_201_CREATED,
        )

    def delete(self, request):
        key = request.query_params.get("key") or ""
        store_theme = self.store_theme()
        if key not in page_template_keys(store_theme.custom_data):
            raise ValidationError({"key": "Unknown page template."})
        delete_path(store_theme, f"templates/{key}.liquid")
        return Response({"templates": page_templates(store_theme)})


def _preview_target(request):
    """
    (page, render_page arguments, message) for a theme preview page.
    The product page shows ?product=<id> or the newest product; the collection page shows all products;
    the article page shows ?article=<handle> or the newest post. The cart is rendered from the existing
    cart snippet, so it uses the home page's theme data rather than a saved cart template.
    message: why the page can't be shown (no product to show), else None.
    """
    page = request.query_params.get("page") or "index"
    if page == "cart":
        return "index", {"cart_page": True}, None
    if not is_page_key(page):
        raise ValidationError({"page": f"page must be one of: {', '.join(PAGES)}, or a page.<name> template."})
    if page == "collection":
        # ?collection=<handle or id>: that collection; otherwise all products
        value = (request.query_params.get("collection") or "").strip()
        chosen = None
        if value:
            try:
                chosen = published_collections(request.store).filter(pk=uuid.UUID(value)).first()
            except ValueError:
                chosen = published_collections(request.store).filter(handle=value).first()
        collection_data = {"name": chosen.name, "obj": chosen} if chosen is not None else {"name": "Search results" if request.query_params.get("q") else "All products", "obj": None}
        target = {
            "collection": collection_data,
            "filters": parse_collection_filters(request.query_params),
            "sort": request.query_params.get("sort", "newest"),
            "page_number": int(request.query_params.get("page", 1)) if str(request.query_params.get("page", "")).isdigit() else 1,
        }
        return page, target, None
    if page == "product":
        products = published_products(request.store)
        try:
            chosen = products.filter(pk=uuid.UUID(request.query_params.get("product", ""))).first()
        except ValueError:
            chosen = None
        product = chosen or products.order_by("-created_at").first()
        if product is None:
            return page, {}, "Add an active product on the Online Store to preview the product page."
        return page, {"product": product}, None
    if base_page(page) == "page":
        # ?handle=<handle>: that page; otherwise the first visible one, or a sample
        return page, {"page_handle": request.query_params.get("handle") or None}, None
    if page == "article":
        # ?article=<handle>: that post; otherwise the newest
        return page, {"article": request.query_params.get("article") or None}, None
    return page, {}, None


class ThemePreviewView(StoreThemeView):
    """
    POST /api/v1/themes/preview/?page=&product=&scroll=&selected=  {unsaved custom_data} -> {"html": ...}
    Renders a page (Home by default) with the customizer's unsaved sections,
    for its live preview, with drag handles on every section. Nothing is saved.
    """

    action_permissions = {"POST": "themes.view"}

    def post(self, request):
        preview_data = request.data
        if isinstance(request.data, dict):
            # The customizer may attach an empty, temporary cart entry while switching preview pages
            # (and keeps it when moving on to another page, e.g. checkout).
            # Cart markup is a shared snippet, not an editable/saved template, so omit that entry
            # before validating the theme data.
            templates = request.data.get("templates")
            if isinstance(templates, dict) and "cart" in templates:
                preview_data = {**request.data, "templates": {**templates}}
                preview_data["templates"].pop("cart")
        clean = validate_custom_data(preview_data)
        page, target, message = _preview_target(request)
        # ?customer=login|register|account: the customer page (templates/login.liquid) with the theme's header and
        # footer. The account page shows sample customer data (the editor never uses a real shopper's sign-in).
        customer_page = request.query_params.get("customer")
        if customer_page in ("login", "register", "account"):
            page, target, message = "index", {"customer_page": customer_page}, None
        if message:
            return Response({"html": f"<p style='padding:24px;font:15px system-ui;color:#53627E'>{escape(message)}</p>",
                             "fonts": ""})

        # ?section=<id>: just that section, to swap into the open preview (plus the fonts it may need)
        section_id = request.query_params.get("section")
        if section_id in sync.SYNCED_SECTIONS:
            started = time.perf_counter()
            html = render_synced_section_fast(request.store, section_id, clean)
            if html is not None:
                sync.TIMINGS.add(f"{section_id}: server render", (time.perf_counter() - started) * 1000)
                return Response({"html": str(html), "fonts": ""})
        if section_id:
            try:
                html = render_page(request.store, page, custom_data=clean, editor=True, only_section=section_id,
                                   cart_id=cart_data.preview_id(request.store, request.user), **target)
            except LiquidError as error:
                html = f"<pre style='padding:24px;color:#b42318'>Liquid error: {escape(str(error))}</pre>"
            return Response({"html": html, "fonts": google_fonts_link(_collect_fonts(resolve(clean)))})

        try:
            html = render_page(request.store, page, custom_data=clean, editor=True,
                               cart_id=cart_data.preview_id(request.store, request.user), **target)
        except LiquidError as error:
            html = f"<pre style='padding:24px;color:#b42318'>Liquid error: {escape(str(error))}</pre>"

        # Store links are site-relative (/s/<slug>/...); resolve them against the API host
        # With Edit mode off, links open inside the preview, like on the real store
        base = f'<base href="{escape(request.build_absolute_uri("/"))}" target="_self">'
        html = html.replace("<head>", "<head>" + base, 1) if "<head>" in html else base + html

        try:
            scroll = max(0, int(request.query_params.get("scroll", 0)))
        except ValueError:
            scroll = 0
        config = json.dumps({"scroll": scroll, "selected": request.query_params.get("selected", "")[:60]})
        # "<" can't appear in the JSON, so it can't close the script tag
        editor = PREVIEW_EDITOR.replace("__CONFIG__", config.replace("<", "\\u003c"))
        html = html.replace("</body>", editor + "</body>", 1) if "</body>" in html else html + editor

        return Response({"html": html})


_schema_etag = None


class ThemeSchemaView(APIView):
    """
    GET /api/v1/themes/schema/

    Returns every section type and its settings.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        # The same for every store and unchanged until the next deploy: the browser keeps it and asks only
        # whether it changed (If-None-Match), instead of downloading it on every editor load.
        global _schema_etag
        if _schema_etag is None:
            _schema_etag = '"%s"' % hashlib.sha1(json.dumps(public_schema(), sort_keys=True, default=str).encode()).hexdigest()
        headers = {"ETag": _schema_etag, "Cache-Control": "private, max-age=300, must-revalidate"}
        if _schema_etag in [tag.strip() for tag in request.headers.get("If-None-Match", "").split(",")]:
            return Response(status=status.HTTP_304_NOT_MODIFIED, headers=headers)
        return Response(public_schema(), headers=headers)


# ------------------------------------------------------------
# Theme CSS
# ------------------------------------------------------------

def theme_css(request, theme_id):
    """
    GET /api/v1/themes/theme-css/<theme_id>/

    Generates CSS variables from the theme's theme_data.
    """

    from .models import Theme

    theme = get_object_or_404(
        Theme,
        id=theme_id,
    )

    css = generate_css_variables(
        theme.theme_data
    )

    return HttpResponse(
        css,
        content_type="text/css",
    )


# ------------------------------------------------------------
# Storefront
# ------------------------------------------------------------

# ------------------------------------------------------------
# "Back to admin" bar
#
# "View store" in the admin asks StorefrontLinkView for a link with a
# short-lived signed token. The storefront swaps it for a cookie limited to
# that store's pages (this browser session), then shows a slim bar on top of
# every store page - only while the user is still an active member of the
# store (owner, admin or staff). Shoppers and logged-out visitors never have
# the cookie, so they never see the bar.
# ------------------------------------------------------------

ADMIN_BAR_SALT = "themes.admin-bar"
ADMIN_BAR_COOKIE = "tss_admin_bar"
ADMIN_BAR_LINK_AGE = 5 * 60  # seconds the "View store" link works
ADMIN_BAR_VISIT_AGE = 12 * 60 * 60  # a visit lasts at most this long
ADMIN_HOME = "/admin/online-store/home"


def _admin_origins():
    return [o.rstrip("/") for o in [settings.FRONTEND_URL, *getattr(settings, "CORS_ALLOWED_ORIGINS", [])] if o]


def _safe_return_url(url):
    """An admin page on this site's frontend; otherwise the admin Home."""
    url = str(url or "")
    if any(url == o or url.startswith(o + "/") for o in _admin_origins()):
        return url
    return settings.FRONTEND_URL.rstrip("/") + ADMIN_HOME


def _member_visit(store, data):
    """The signed visit data if it is for this store and the user still belongs to it."""
    if not isinstance(data, dict) or data.get("s") != str(store.store_id):
        return None
    active = StoreMembership.objects.filter(user_id=data.get("u"), store=store, status="active").exists()
    # A platform admin may visit any store without being a member of it
    active = active or get_user_model().objects.filter(pk=data.get("u"), is_owner=True).exists()
    return data if active else None


def _admin_visit(request, store):
    try:
        data = signing.loads(request.COOKIES.get(ADMIN_BAR_COOKIE, ""), salt=ADMIN_BAR_SALT, max_age=ADMIN_BAR_VISIT_AGE)
    except signing.BadSignature:
        return None
    return _member_visit(store, data)


def _preview_theme_id(store, theme_id):
    """The id of one of this store's own themes, or None (unknown, or not a uuid)."""
    try:
        found = StoreTheme.objects.select_related("theme").filter(store=store, theme_id=theme_id).first()
    except (ValueError, DjangoValidationError):
        return None
    return str(found.theme_id) if found and is_private(store, found.theme) else None


def _admin_bar_redirect(request, store):
    """
    ?admin_bar=<token>: start a visit (sets the cookie). ?admin_bar_hide=1: hide
    the bar for the rest of the visit. Either way, go on to the clean URL.
    """
    params = request.GET.copy()
    token = params.pop("admin_bar", [None])[0]
    hide = params.pop("admin_bar_hide", None) is not None
    response = HttpResponseRedirect(request.path + (f"?{params.urlencode()}" if params else ""))

    if token is not None:
        try:
            visit = _member_visit(store, signing.loads(token, salt=ADMIN_BAR_SALT, max_age=ADMIN_BAR_LINK_AGE))
        except signing.BadSignature:
            visit = None
    else:
        visit = _admin_visit(request, store)

    if visit is not None:
        response.set_cookie(
            ADMIN_BAR_COOKIE,
            signing.dumps({**visit, "h": hide}, salt=ADMIN_BAR_SALT),
            path=f"/s/{store.store_slug}/",  # this store's pages only
            httponly=True,
            samesite="Lax",
            secure=request.is_secure(),
        )
    return response


ADMIN_BAR_HTML = """<div id="tss-admin-bar" role="region" aria-label="Store admin">
<style>
#tss-admin-bar{{position:relative;z-index:2147483000;display:flex;align-items:center;gap:8px;height:40px;padding:0 6px 0 8px;
background:#141b2d;color:#fff;font:500 14px/1 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;box-sizing:border-box}}
#tss-admin-bar a,#tss-admin-bar button{{display:inline-flex;align-items:center;gap:6px;height:30px;padding:0 10px;border:0;border-radius:6px;
background:transparent;color:#fff;font:inherit;text-decoration:none;cursor:pointer;white-space:nowrap;transition:background .15s}}
#tss-admin-bar a:hover,#tss-admin-bar button:hover,#tss-admin-bar a:focus-visible,#tss-admin-bar button:focus-visible{{background:rgba(255,255,255,.14);outline:none}}
#tss-admin-bar span{{min-width:0;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;opacity:.8}}
#tss-admin-bar button{{padding:0 10px;font-size:20px;line-height:1}}
</style>
<a href="{back}"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M19 12H5M12 19l-7-7 7-7"/></svg>Back to admin</a>
<span>{name}</span>
<button type="button" aria-label="Hide this bar" title="Hide for this visit"
onclick="document.getElementById('tss-admin-bar').remove();var u=new URL(location.href);u.searchParams.set('admin_bar_hide','1');location.replace(u.href)">&times;</button>
</div>"""


def _with_admin_bar(html, store, visit):
    """The bar goes first inside <body>, in the page flow: it pushes the store's own header down."""
    bar = ADMIN_BAR_HTML.format(back=escape(_safe_return_url(visit.get("r"))), name=escape(store.store_name))
    html, found = re.subn(r"<body\b[^>]*>", lambda m: m.group(0) + bar, str(html), count=1, flags=re.IGNORECASE)
    return html if found else bar + html


class StorefrontLinkView(APIView):
    """
    POST /api/v1/themes/storefront-link/  {"path": "/products/<id>", "return_url": "<admin page>"}
    -> {"url": ...}: the active store's storefront, with the "Back to admin" bar for this user.
    """

    permission_classes = [IsAuthenticated, IsStoreMember, HasStorePermission]
    permission_resource = "themes"
    # Looking at the store is for every member, whatever their role (None = any member)
    action_permissions = {"POST": None}

    def post(self, request):
        path = str(request.data.get("path") or "/")
        if not path.startswith("/") or path.startswith("//"):
            path = "/"
        visit = {
            "u": str(request.user.pk),
            "s": str(request.store.store_id),
            "r": _safe_return_url(request.data.get("return_url")),
        }
        # "Open theme" on a draft: this visit sees that draft instead of the published theme
        theme_id = request.data.get("theme_id")
        if theme_id:
            draft = _preview_theme_id(request.store, theme_id)
            if draft is None:
                raise exceptions.NotFound("That theme doesn't exist.")
            visit["t"] = draft
        token = signing.dumps(visit, salt=ADMIN_BAR_SALT)
        url = request.build_absolute_uri(shop_url(request.store) + path)
        return Response({"url": url + ("&" if "?" in url else "?") + urlencode({"admin_bar": token})})


REDIRECT_HOPS = 5  # a redirect that leads to another one is followed this far, then given up on


def _redirect_forms(store, path):
    """The ways a redirect from `path` may have been saved (see rendering.redirect_forms)."""
    return redirect_forms(store, path)


def _store_redirect(store, path, query=""):
    """
    Where a page that was not found should send the shopper: this store's UrlRedirect for the path (301), followed
    through further redirects. None when there is none, it loops, it takes more than REDIRECT_HOPS, or the target
    is not a store path or an http(s) link.
    """
    base = shop_url(store)
    seen, current, hops = {_redirect_forms(store, path)[0]}, path, 0
    while True:
        row = (
            UrlRedirect.objects.filter(store=store, path__in=_redirect_forms(store, current)[1])
            .order_by("-created_at").first()
        )
        if row is None:
            break
        hops += 1
        target = (row.target or "").strip()
        if hops > REDIRECT_HOPS or "\n" in target or "\r" in target:
            return None
        if target.startswith(("http://", "https://")):
            return target  # another site: the end of the road
        if not target.startswith("/") or target.startswith("//"):
            return None
        current = target if target == base or target.startswith(base + "/") else base + target
        key = _redirect_forms(store, current)[0]
        if key in seen:
            return None  # a loop
        seen.add(key)
    if hops == 0:
        return None
    return current + ("?" + query if query and "?" not in current else "")


def _storefront(render):
    """
    Wraps a public storefront view.

    Theme code is written by store owners, so the page is served
    with a CSP sandbox. It runs as an isolated origin and cannot
    use this site's cookies or API.

    It may be framed for the editor's live preview.
    """

    @xframe_options_exempt
    @require_GET
    def view(request, store_slug, **kwargs):
        store = get_object_or_404(
            Store,
            store_slug=store_slug,
        )

        if "admin_bar" in request.GET or "admin_bar_hide" in request.GET:
            return _admin_bar_redirect(request, store)

        # A visit started from "Open theme" on a draft shows that draft on every page of the visit
        visit = _admin_visit(request, store)
        request._no_track = visit is not None  # staff looking at their own store are not visitors
        draft = _preview_theme_id(store, visit["t"]) if visit and visit.get("t") else None
        editing = set_editing_theme(draft) if draft else None

        try:
            html = render(store, request, request.GET, **kwargs)

            status_code = 200

        except Http404:
            moved = _store_redirect(store, request.path, request.META.get("QUERY_STRING", ""))
            if moved:
                return HttpResponsePermanentRedirect(moved)
            # Drafts and unknown pages: a readable page that the preview frame is allowed to show
            html = (
                "<!doctype html><meta charset='utf-8'><title>Page not found</title>"
                "<div style='font:16px/1.5 system-ui,sans-serif;padding:64px 24px;text-align:center;color:#161C2C'>"
                "<h1 style='font-size:24px'>This page isn't available</h1>"
                "<p>The product or collection is not published, or it no longer exists.</p></div>"
            )

            status_code = 404

        except LiquidError as error:
            html = (
                "<!doctype html>"
                "<meta charset='utf-8'>"
                "<title>Theme error</title>"
                "<pre style='font:14px/1.5 monospace;"
                "padding:24px;color:#b42318'>"
                f"Liquid error: {escape(str(error))}"
                "</pre>"
            )

            status_code = 500

        finally:
            if editing is not None:
                reset_editing_theme(editing)

        if isinstance(html, (HttpResponse, JsonResponse)):
            return html

        # Store members who came from "View store" get the "Back to admin" bar.
        # Never inside a frame (the customizer's live preview).
        if visit is not None and not visit.get("h") and request.headers.get("Sec-Fetch-Dest") != "iframe":
            html = _with_admin_bar(html, store, visit)
        html = privacy.inject(html, store, request)

        response = HttpResponse(
            html,
            status=status_code,
        )

        response["Content-Security-Policy"] = (
            "sandbox allow-scripts allow-forms "
            "allow-popups allow-popups-to-escape-sandbox"
        )

        response["Cache-Control"] = "no-store"
        _restore_customer_cookie(response, request, store)

        # A page with the cart on it gives the shopper's browser their cart id (see cart.py)
        _give_cart_cookie(response, request, store, html)

        return response

    return view


@_storefront
def storefront_home(store, request, params):
    return render_page(
        store,
        "index",
        request=request,
    )


@_storefront
def storefront_not_found(store, request, params, rest=""):
    """Any other address under the store: a redirect for it if the store has one, else the not-found page."""
    raise Http404("Page not found.")


@_storefront
def storefront_product(
    store,
    request,
    params,
    product_id,
):
    # Same rule as the product lists: Active and on the Online Store
    product = published_products(store).filter(pk=product_id).first()

    if product is None:
        raise Http404(
            "Product not found."
        )

    return render_page(
        store,
        "product",
        product=product,
        request=request,
    )


def _listing(params):
    """?sort=newest|price-asc|price-desc&page=N, plus the filters (see parse_collection_filters)"""
    try:
        page_number = int(params.get("page", 1))
    except ValueError:
        page_number = 1
    return {
        "sort": params.get("sort", "newest"),
        "page_number": page_number,
        "filters": parse_collection_filters(params),
    }


@_storefront
def storefront_collection(
    store,
    request,
    params,
    collection_id=None,
    handle=None,
):
    if collection_id is None and handle is None:
        return render_page(
            store,
            "collection",
            collection={
                "name": "All products",
                "obj": None,
            },
            request=request,
            **_listing(params),
        )


    # Draft collections, and ones off the Online Store, are not found
    lookup = {"pk": collection_id} if collection_id is not None else {"handle": handle}
    collection = published_collections(store).filter(**lookup).first()
    if collection is None and handle == "promotions":
        collection = Collection(
            store=store,
            name="Promotions",
            handle="promotions",
            description="Welcome! Promotional products and special offers will appear here soon.",
        )
    elif collection is None:
        raise Http404("Collection not found.")

    return render_page(
        store,
        "collection",
        collection={
            "name": collection.name,
            "obj": collection,
        },
        request=request,
        **_listing(params),
    )


@_storefront
def storefront_search(store, request, params):
    """Storefront search: renders the collection template with products matching ?q=... (or JSON if requested)."""
    if request.headers.get("Accept") == "application/json" or params.get("format") == "json":
        all_products = _products(store, shop_url(store))
        filters = _with_category_ids(store, parse_collection_filters(params))
        matched = [p for p in all_products if _matches_filters(p, filters)]
        results_list = [
            {
                "id": str(p["id"]),
                "title": p["title"],
                "url": p["url"],
                "price": p["price"],
                "featured_image": p["featured_image"],
                "vendor": p["vendor"],
                "available": not p["is_sold_out"],
            }
            for p in matched[:20]
        ]
        return JsonResponse({
            "query": filters.get("q", ""),
            "q": filters.get("q", ""),
            "total": len(matched),
            "results": results_list,
            "products": results_list,
        })
    return render_page(
        store,
        "collection",
        collection={
            "name": f"Search results for \u201c{params.get('q')}\u201d" if params.get("q") else "All products",
            "obj": None,
        },
        request=request,
        **_listing(params),
    )


BLOG_FILTERS = ("category", "tagged", "author")


@_storefront
def storefront_blog(store, request, params, kind=None, handle=None):
    """/blog, and /blog/category|tagged|author/<handle>: the published posts, newest first."""
    try:
        page_number = int(params.get("page", 1))
    except ValueError:
        page_number = 1
    blog_filter = None
    if kind is not None:
        if kind not in BLOG_FILTERS:
            raise Http404("Page not found.")
        blog_filter = {"kind": kind, "handle": handle}
    return render_page(store, "blog", blog_filter=blog_filter, page_number=page_number, request=request)


@_storefront
def storefront_article(store, request, params, handle):
    """/blog/<handle>: one published post; drafts and unknown handles are not found."""
    if not published_posts(store).filter(handle=handle).exists():
        raise Http404("Post not found.")
    return render_page(store, "article", article=handle, request=request)


@_storefront
def storefront_wishlist(store, request, params):
    del params
    # The shopper's cart id (cookie, or the link's ?cart=): the sandboxed page cannot send its session cookie
    cart_session, cart_new = cart_data.session_for_page(store, request)
    if cart_new:
        request._cart_session = (cart_session, True)
    ids = cart_data.get_wishlist(store, request, cart_session)
    valid_ids = []
    for value in ids:
        try:
            normalized = str(uuid.UUID(str(value)))
        except (ValueError, TypeError, AttributeError):
            continue
        if normalized not in valid_ids:
            valid_ids.append(normalized)
    if valid_ids != ids:
        cart_data.put_wishlist(store, request, cart_session, valid_ids)

    products = _catalog(store).filter(pk__in=valid_ids)
    ordered_products = {str(product.pk): product for product in products}
    visible_ids = [product_id for product_id in valid_ids if product_id in ordered_products]  # saved ones stay saved
    return render_page(
        store,
        "index",
        request=request,
        cart_id=cart_session,
        wishlist_page_ids=visible_ids,
        wishlist_products=ordered_products,
    )


logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ the cart (see cart.py and snippets/cart.liquid)

# The storefront page runs in a sandbox (no cookies, no same-origin), so its script reaches these endpoints
# as a foreign origin: they answer CORS themselves. Nothing here trusts the caller beyond the cart id it was
# given, which is random: a request for another shopper's cart cannot be made without knowing that id.
CART_API_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "X-Cart-Session, Content-Type, Accept",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Max-Age": "600",
    "Cache-Control": "no-store",
}
CART_MAX_AT_ONCE = 99


def _first_message(detail):
    if isinstance(detail, dict):
        return _first_message(next(iter(detail.values()), "Something went wrong."))
    if isinstance(detail, (list, tuple)):
        return _first_message(detail[0]) if detail else "Something went wrong."
    return str(detail)


def _cart_api(view):
    """JSON endpoints of one store's cart: view(request, store, cart_id) -> JsonResponse; ValidationError -> 400."""

    @csrf_exempt
    def wrapper(request, store_slug):
        if request.method == "OPTIONS":
            response = HttpResponse(status=204)
        elif request.method not in ("GET", "POST"):
            response = JsonResponse({"error": "Method not allowed."}, status=405)
        else:
            store = Store.objects.filter(store_slug=store_slug, status="active").first()
            if store is None:
                response = JsonResponse({"error": "Store not found."}, status=404)
            else:
                cart_id = cart_data.clean_id(request.headers.get("X-Cart-Session", ""))
                try:
                    response = view(request, store, cart_id)
                except ValidationError as error:
                    response = JsonResponse({"error": _first_message(error.detail)}, status=400)
                except LiquidError as error:
                    response = JsonResponse({"error": f"Theme error: {error}"}, status=500)
        for name, value in CART_API_HEADERS.items():
            response[name] = value
        return response

    return wrapper


def _json_body(request):
    try:
        body = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        raise ValidationError("That request could not be read.")
    if not isinstance(body, dict):
        raise ValidationError("That request could not be read.")
    return body


class _TrackThrottle(SimpleRateThrottle):
    """The storefront's rate ("storefront" in REST_FRAMEWORK), counted per client address apart from the cart API's."""
    scope = "storefront"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": "track-" + self.get_ident(request)}


TRACK_MAX_BODY = 4096


def _track_origin_ok(request):
    """The beacon comes from the sandboxed storefront page (Origin: null), or the store's own site; nobody else."""
    origin = request.headers.get("Origin")
    return not origin or origin == "null" or _origin_allowed(request)


@csrf_exempt
def storefront_track(request, store_slug):
    """
    POST /s/<store_slug>/_track: one page load of the storefront (or, with {"type": "social_click", "url": ...},
    one click on a social link), sent by the layout's script with navigator.sendBeacon. Always answers 204 and nothing else, whatever the body: bots, editor previews
    and bad payloads are dropped quietly. See apps.orders.orders_services.
    """
    if request.method == "OPTIONS":
        response = HttpResponse(status=204)
    elif request.method != "POST":
        response = HttpResponse(status=405)
    elif not _track_origin_ok(request):
        response = HttpResponse(status=403)
    elif not _TrackThrottle().allow_request(request, None):
        response = HttpResponse(status=429)
    else:
        response = HttpResponse(status=204)
        response["X-Visit-Tracking"] = "ignored"  # visible in the browser's network tab: counted or not
        user_agent = request.headers.get("User-Agent", "")
        too_big = int(request.META.get("CONTENT_LENGTH") or 0) > TRACK_MAX_BODY
        if not orders_services.is_bot(user_agent) and not too_big:
            store = Store.objects.filter(store_slug=store_slug, status="active").first()
            try:
                body = json.loads(request.body.decode("utf-8")) if store else None
            except (ValueError, UnicodeDecodeError):
                body = None
            if store is not None and isinstance(body, dict):
                try:
                    host = request.get_host()
                except DisallowedHost:
                    host = ""
                if body.get("type") == "social_click":
                    counted = orders_services.record_social_click(
                        store, url=body.get("url"), user_agent=user_agent, own_host=host) or None
                else:
                    counted = orders_services.record_pageview(
                        store,
                        visitor_id=body.get("visitor_id"), path=body.get("path"), referrer=body.get("referrer"),
                        utm_source=body.get("utm_source"), utm_medium=body.get("utm_medium"),
                        utm_campaign=body.get("utm_campaign"),
                        cart_session=cart_data.clean_id(str(body.get("cart_session") or "")),
                        user_agent=user_agent, country=request.META.get("HTTP_CF_IPCOUNTRY", ""), own_host=host,
                    )
                if counted is not None:
                    response["X-Visit-Tracking"] = "counted"
    for name, value in CART_API_HEADERS.items():
        response[name] = value
    return response


def _uuid_of(value, message):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError(message)


def _whole(value, message, minimum):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValidationError(message)
    return value


def _cart_reply(store, cart):
    """The count, the total and the cart's lines as HTML: what the page's script puts back on the page."""
    data = cart_data.view_data(cart, shop_url(store))
    opts = cart_data.options(get_store_theme(store))
    return JsonResponse({"count": data["count"], "subtotal": data["subtotal"], "html": render_cart_html(store, data, opts)})


@_cart_api
def storefront_cart_fragment(request, store, cart_id):
    if request.method != "GET":
        raise ValidationError("Use GET.")
    return _cart_reply(store, cart_data.open_cart(store, cart_id))


@_cart_api
def storefront_cart_add(request, store, cart_id):
    if request.method != "POST":
        raise ValidationError("Use POST.")
    if not cart_id:
        raise ValidationError("Your cart is not available. Reload the page and try again.")
    body = _json_body(request)
    quantity = _whole(body.get("quantity", 1), "Choose a quantity of 1 or more.", 1)
    if quantity > CART_MAX_AT_ONCE:
        raise ValidationError(f"You can add up to {CART_MAX_AT_ONCE} at a time.")
    product = Product.objects.filter(pk=_uuid_of(body.get("product"), "This product isn't available."), store=store).first()
    if product is None:
        raise ValidationError("This product isn't available.")
    variant = None
    if body.get("variant"):
        variant = ProductVariant.objects.filter(pk=_uuid_of(body["variant"], "Choose an option."), product=product).first()
        if variant is None:
            raise ValidationError("Choose an option.")
    cart = commerce_services.get_or_create_cart(store, session_id=cart_id)
    commerce_services.add_item(cart, product, quantity, variant)  # published, in stock, price from the variant
    orders_services.mark_cart_event(store, cart_id, added_to_cart=True)
    return _cart_reply(store, cart)


@_cart_api
def storefront_newsletter(request, store, cart_id):
    """The "Newsletter" section's form: subscribes an email address to the store's marketing emails."""
    if request.method != "POST":
        raise ValidationError("Use POST.")
    body = _json_body(request)
    email = str(body.get("email") or "").strip().lower()
    if not email or len(email) > 320 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValidationError("Enter a valid email address.")
    customer = Customer.objects.filter(store=store, email__iexact=email).first()
    if customer is None:
        try:
            customer = Customer.objects.create(store=store, email=email, email_subscription_status="not_subscribed")
        except IntegrityError:
            customer = Customer.objects.filter(store=store, email__iexact=email).first()
    if customer is not None and customer.email_subscription_status != "subscribed":
        privacy.record_consent(store, customer, request, email=True)
    return JsonResponse({"ok": True, "confirm": bool(store.double_opt_in_email)})


@_cart_api
def storefront_cart_change(request, store, cart_id):
    if request.method != "POST":
        raise ValidationError("Use POST.")
    body = _json_body(request)
    quantity = _whole(body.get("quantity"), "Choose a quantity of 0 or more.", 0)
    if quantity > CART_MAX_AT_ONCE:
        raise ValidationError(f"You can have up to {CART_MAX_AT_ONCE} of one product.")
    cart = cart_data.open_cart(store, cart_id)
    line = _uuid_of(body.get("line"), "That item isn't in your cart.")
    if cart is None or not cart.items.filter(pk=line, is_deleted=False).exists():
        # Already gone (removed in another tab, or the cart was emptied): removing it again is not an error, and
        # the reply refreshes the page's stale lines
        if quantity == 0:
            return _cart_reply(store, cart)
        raise ValidationError("That item isn't in your cart. It may have been removed.")
    commerce_services.set_quantity(cart, line, quantity)
    return _cart_reply(store, cart)


@_cart_api
def storefront_cart_clear(request, store, cart_id):
    if request.method != "POST":
        raise ValidationError("Use POST.")
    cart = cart_data.open_cart(store, cart_id)
    if cart is not None:
        commerce_services.clear_cart(cart)
    return _cart_reply(store, cart)


@_storefront
def storefront_cart(store, request, params):
    return render_page(store, "index", request=request, cart_page=True)


def _with_cart_cors(view):
    """The checkout page's script runs sandboxed too: answer its preflight and tag every reply, as the cart API does."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        response = HttpResponse(status=204) if request.method == "OPTIONS" else view(request, *args, **kwargs)
        for name, value in CART_API_HEADERS.items():
            if name != "Cache-Control":
                response[name] = value
        return response

    return wrapper


def _record_checkout_consent(store, request, email, data):
    """Marketing boxes ticked at checkout. Only channels the store asks about count; failures never block the order."""
    def ticked(key, mode):
        value = data.get(key)
        return mode != "hidden" and value in (True, "true", "on", "1", 1)
    wants_email = ticked("email_marketing", store.checkout_email_marketing)
    wants_sms = ticked("sms_marketing", store.checkout_sms_marketing)
    if not (wants_email or wants_sms):
        return
    try:
        customer = Customer.objects.filter(store=store, email__iexact=email).first()
        privacy.record_consent(store, customer, request, email=wants_email, sms=wants_sms)
    except Exception:
        logger.exception("Could not record marketing consent for store %s", store.pk)


@xframe_options_exempt
@csrf_exempt
@_with_cart_cors
def storefront_checkout(request, store_slug):
    store = get_object_or_404(Store, store_slug=store_slug)
    cart_session, cart_new = cart_data.session_for_page(store, request)
    if cart_new:
        request._cart_session = (cart_session, True)

    # Settings > Checkout: shoppers must sign in first
    if store.checkout_require_login and request.method in ("GET", "POST") and get_storefront_customer(store, request)[0] is None:
        if request.method == "GET":
            return HttpResponseRedirect(f"{shop_url(store)}/account/login?return_to={quote('/checkout')}")
        return JsonResponse({"error": "Sign in to your account to check out."}, status=403)

    if request.method == "POST":
        is_json = "application/json" in request.headers.get("Accept", "") or request.content_type == "application/json"
        data = _json_body(request) if request.content_type == "application/json" else request.POST
        post_session = cart_data.clean_id(data.get("cart_session") or data.get("cart") or request.headers.get("X-Cart-Session") or "")
        if post_session:
            cart_session = post_session
        action = data.get("action", "place_order")
        cart = cart_data.open_cart(store, cart_session)

        if action == "apply_discount":
            if cart is None or not cart.items.filter(is_deleted=False).exists():
                return JsonResponse({"error": "Your cart is empty."}, status=400)
            code = (data.get("discount_code") or "").strip()
            try:
                checkout = commerce_services.start_checkout(cart, discount_code=code)
                return JsonResponse({
                    "success": True,
                    "discount_amount": str(checkout.discount_amount),
                    "shipping_amount": str(checkout.shipping_amount),
                    "tax_amount": str(checkout.tax_amount),
                    "subtotal": str(checkout.subtotal),
                    "total_amount": str(checkout.total_amount),
                })
            except Exception as e:
                msg = _first_message(getattr(e, "detail", str(e)))
                return JsonResponse({"error": msg}, status=400)

        # Place order
        if cart is None or not cart.items.filter(is_deleted=False).exists():
            if is_json:
                return JsonResponse({"error": "Your cart is empty."}, status=400)
            html = render_page(store, "checkout", request=request, cart_id=cart_session, auth_error="Your cart is empty.")
            return _storefront_response(html, status=400, request=request, store=store)

        email = (data.get("email") or "").strip().lower()
        first_name = (data.get("first_name") or "").strip()
        last_name = (data.get("last_name") or "").strip()
        address1 = (data.get("address1") or data.get("address_line_1") or "").strip()
        address2 = (data.get("address2") or data.get("address_line_2") or "").strip()
        city = (data.get("city") or "").strip()
        state = (data.get("state") or "").strip()
        postal_code = (data.get("postal_code") or "").strip()
        country = (data.get("country") or "India").strip()
        phone = (data.get("phone") or "").strip()
        payment_method = (data.get("payment_method") or "cod").strip().lower()
        if payment_method in ("cash_on_delivery", "cash-on-delivery", "cash on delivery"):
            payment_method = "cod"
        discount_code = (data.get("discount_code") or "").strip()
        notes = (data.get("notes") or data.get("order_notes") or "").strip()

        error_field = ""
        try:
            email = commerce_services.clean_email(email)
            error = None
        except ValidationError as e:
            error, error_field = _first_message(e.detail), "email"
        if error:
            pass
        elif not last_name or (store.checkout_full_name != "last_only" and not first_name):
            error = "Last name is required." if store.checkout_full_name == "last_only" else "First and last name are required."
        elif not address1 or not city or not postal_code:
            error = "Delivery address, city and postal code are required."
        elif store.checkout_company == "required" and not (data.get("company") or "").strip():
            error = "Company name is required."
        elif store.checkout_address2 == "required" and not address2:
            error = "Apartment, suite or unit is required."
        elif store.checkout_phone == "required" and not phone:
            error = "Phone number is required."
        else:
            error = None

        if error:
            if is_json:
                return JsonResponse({"error": error, "field": error_field}, status=400)
            html = render_page(store, "checkout", request=request, cart_id=cart_session, auth_error=error)
            return _storefront_response(html, status=400, request=request, store=store)

        try:
            shipping_address = {
                "first_name": first_name,
                "last_name": last_name,
                "company": (data.get("company") or "").strip() if store.checkout_company != "hidden" else "",
                "address1": address1,
                "address2": address2 if store.checkout_address2 != "hidden" else "",
                "city": city,
                "province": state,
                "state": state,
                "postal_code": postal_code,
                "country": country,
                "phone": phone if store.checkout_phone != "hidden" else "",
            }
            checkout = commerce_services.start_checkout(
                cart,
                email=email,
                shipping_address=shipping_address,
                billing_address=shipping_address,
                discount_code=discount_code,
            )
            if notes:
                metadata = dict(checkout.metadata or {})
                metadata["notes"] = notes
                checkout.metadata = metadata
                checkout.save(update_fields=["metadata"])

            order = commerce_services.place_order(checkout, payment_method=payment_method)
            _record_checkout_consent(store, request, email, data)
            if is_json:
                return JsonResponse({
                    "success": True,
                    "order_id": str(order.pk),
                    "order_number": getattr(order, "order_number", str(order.pk)[:8]),
                    "total": str(order.total),
                    "email": email,
                })
            return HttpResponseRedirect(f"{shop_url(store)}/checkout/order/{order.pk}/")
        except Exception as e:
            msg = _first_message(getattr(e, "detail", str(e)))
            if is_json:
                return JsonResponse({"error": msg, "field": "email" if "email address" in msg else ""}, status=400)
            html = render_page(store, "checkout", request=request, cart_id=cart_session, auth_error=msg)
            return _storefront_response(html, status=400, request=request, store=store)

    if request.method == "GET" and cart_data.item_count(store, cart_session):
        orders_services.mark_cart_event(store, cart_session, reached_checkout=True)
    html = render_page(store, "checkout", request=request, cart_id=cart_session)
    return _storefront_response(html, status=200, request=request, store=store)


@xframe_options_exempt
def storefront_checkout_order(request, store_slug, order_id):
    store = get_object_or_404(Store, store_slug=store_slug)
    order = get_object_or_404(Order, store=store, pk=order_id)
    from apps.commerce.models import Checkout
    checkout = Checkout.objects.filter(store=store, metadata__order_id=str(order.pk)).first()
    shipping_addr = dict(checkout.shipping_address or {}) if checkout and checkout.shipping_address else {}
    cust = order.customer
    if cust:
        shipping_addr.setdefault("first_name", cust.first_name or "")
        shipping_addr.setdefault("last_name", cust.last_name or "")
        shipping_addr.setdefault("phone", getattr(cust, "phone_number", ""))
    order_items = [
        {"title": item.product.title, "quantity": item.quantity, "unit_price": str(item.unit_price), "total": str(item.total)}
        for item in order.items.select_related("product").all()
    ]
    order_data = {
        "id": str(order.pk),
        "order_number": getattr(order, "order_number", str(order.pk)[:8]),
        "email": cust.email if cust else getattr(order, "customer_email", ""),
        "total": str(order.total),
        "shipping_address": shipping_addr,
        "payment_method": (checkout.metadata or {}).get("payment_method", "cod") if checkout and checkout.metadata else "cod",
        "items": order_items,
    }
    html = render_page(
        store,
        "checkout",
        request=request,
        order_completed=True,
        order=order_data,
    )
    return _storefront_response(html, status=200, request=request, store=store)


@csrf_exempt
@require_POST
def storefront_wishlist_item(request, store_slug):
    """Update the guest wishlist (kept under the shopper's cart id); these changes do not modify protected customer data."""
    store = get_object_or_404(Store, store_slug=store_slug)
    # The storefront page runs in a sandbox: it sends the cart id its heart was given instead of a cookie
    cart_id = cart_data.clean_id(request.POST.get("cart") or request.headers.get("X-Cart-Session", ""))
    ids = cart_data.get_wishlist(store, request, cart_id)

    action = request.POST.get("action")
    try:
        product_id = str(uuid.UUID(request.POST.get("product_id", "")))
    except (ValueError, TypeError, AttributeError):
        return HttpResponse("Invalid product.", status=400)

    if action == "add":
        if not published_products(store).filter(pk=product_id).exists():
            raise Http404("Product not found.")
        if product_id not in ids:
            ids.append(product_id)
    elif action == "remove":
        ids = [value for value in ids if value != product_id]
    else:
        return HttpResponse("Invalid wishlist action.", status=400)

    cart_data.put_wishlist(store, request, cart_id, ids)
    if "application/json" in request.headers.get("Accept", ""):
        # The wishlist script updates the hearts and the header count without reloading the page
        count = published_products(store).filter(pk__in=ids).count()
        reply = JsonResponse({"saved": action == "add", "count": count, "product_id": product_id})
        reply["Access-Control-Allow-Origin"] = "*"  # the sandboxed page is a foreign origin (no cookies are sent)
        reply["Cache-Control"] = "no-store"
        return reply
    destination = request.POST.get("return_to", "")
    parsed = urlsplit(destination)
    store_path = f"{shop_url(store)}/"
    if parsed.scheme or parsed.netloc or not parsed.path.startswith(store_path):
        destination = f"{shop_url(store)}/wishlist/"
    if cart_id and "cart=" not in destination:
        # a page that reloads in the sandbox keeps the shopper's id (no cookie comes back with it)
        destination += ("&" if "?" in destination else "?") + f"cart={cart_id}"
    return HttpResponseRedirect(destination)


@_storefront
def storefront_page(store, request, params, handle):
    """/pages/<handle>: one visible page; hidden ones and unknown handles are not found."""
    page = published_pages(store).filter(handle=handle).first()
    if page is None:
        raise Http404("Page not found.")
    # The page template the page uses (page.contact); render_page falls back to Page if its file is gone
    return render_page(store, page.template or "page", page_handle=handle, request=request)


# ---------------- Customer Storefront Authentication & Account ----------------

CUSTOMER_COOKIE_PREFIX = "tss_cust_"
CUSTOMER_COOKIE_AGE = 30 * 24 * 60 * 60  # 30 days
CUSTOMER_SALT = "tss_storefront_cust_session_v1"
CUSTOMER_LINK_PARAM = "ct"
CUSTOMER_LINK_AGE = 24 * 60 * 60  # the token a page passes along in its links and forms


def customer_cookie_name(store):
    return f"{CUSTOMER_COOKIE_PREFIX}{store.store_slug}"


def get_storefront_customer(store, request):
    """
    Returns (customer, user) if a customer session is active for this store, else (None, None).
    """
    # The storefront is sandboxed, so its form posts and frames often cannot send the cookie: the page then
    # carries a short-lived signed token in the link (?ct=) or the form (ct), like the cart id
    sources = (
        (request.COOKIES.get(customer_cookie_name(store)) or request.headers.get("X-Customer-Token"), CUSTOMER_COOKIE_AGE),
        (request.GET.get(CUSTOMER_LINK_PARAM) or request.POST.get(CUSTOMER_LINK_PARAM), CUSTOMER_LINK_AGE),
    )
    user = None
    for token, max_age in sources:
        if not token:
            continue
        try:
            data = signing.loads(token, salt=CUSTOMER_SALT, max_age=max_age)
            if data.get("store_id") == str(store.pk):
                user = get_user_model().objects.filter(pk=data.get("user_id")).first()
                # A token from before the last password change (or without the fingerprint) is dead
                if user is not None and not fingerprint_matches(user, data):
                    user = None
        except Exception:
            user = None
        if user is not None:
            break

    if user is None:
        return None, None

    # Known only by the link's 24-hour token: the browser refused or lost the cookie (the login form is a
    # cross-site POST from the sandboxed page). The page's response gives it again (see _restore_customer_cookie).
    if max_age == CUSTOMER_LINK_AGE:
        request._customer_cookie_user = user

    customer = Customer.objects.filter(store=store, email__iexact=user.email).first()
    if customer is None:
        try:
            customer = Customer.objects.create(
                store=store,
                email=user.email,
                first_name=user.first_name,
                last_name=user.last_name,
                email_subscription_status="not_subscribed",
            )
        except IntegrityError:
            customer = Customer.objects.filter(store=store, email__iexact=user.email).first()
    return customer, user


def customer_link_token(store, user):
    return signing.dumps(
        {"user_id": str(user.pk), "store_id": str(store.pk), PASSWORD_CLAIM: password_fingerprint(user)},
        salt=CUSTOMER_SALT,
    )


def signed_in_url(store, user, url):
    """url with the signed-in customer's token, so the next page knows them without the cookie."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != CUSTOMER_LINK_PARAM]
    query.append((CUSTOMER_LINK_PARAM, customer_link_token(store, user)))
    return urlunsplit(parts._replace(query=urlencode(query)))


ACCOUNT_NOTICES = {
    "profile_saved": "Your details were saved.",
    "address_added": "Address saved.",
    "address_exists": "That address is already in your address book.",
    "address_updated": "Address updated.",
    "address_deleted": "Address deleted.",
    "address_default": "Default address updated.",
}
ACCOUNT_ERRORS = {
    "phone_invalid": "Enter a 10-digit mobile number, using numbers only.",
}
MOBILE_NUMBER = re.compile(r"[0-9]{10}")  # exactly 10 digits: no letters, spaces or symbols


def _account_redirect(store, request, user, notice=None):
    """Back to the account page; a customer who came in by token (no cookie) keeps it."""
    url = f"{shop_url(store)}/account"
    if notice in ACCOUNT_NOTICES:
        url += f"?notice={notice}"
    elif notice in ACCOUNT_ERRORS:
        url += f"?error={notice}"
    if user is not None and (request.GET.get(CUSTOMER_LINK_PARAM) or request.POST.get(CUSTOMER_LINK_PARAM)):
        url = signed_in_url(store, user, url)
    return HttpResponseRedirect(url)


def _sync_customer_location(customer):
    """The customer's city, state and country follow their default address (what the admin Customers list shows)."""
    addr = (
        CustomerAddress.objects.filter(customer=customer).order_by("-is_default", "-created_at").first()
    )
    values = {"city": addr.city, "state": addr.state, "country": addr.country} if addr else {"city": None, "state": None, "country": None}
    if any(getattr(customer, k) != v for k, v in values.items()):
        for k, v in values.items():
            setattr(customer, k, v)
        customer.save(update_fields=[*values, "updated_at"])


def _same_address(first, second):
    return all((x or "").strip().lower() == (y or "").strip().lower() for x, y in zip(first, second))


def set_storefront_customer_cookie(response, store, user):
    token = customer_link_token(store, user)
    response.set_cookie(
        customer_cookie_name(store),
        token,
        max_age=CUSTOMER_COOKIE_AGE,
        path=f"{shop_url(store)}/",
        httponly=True,
        samesite="Lax",
        secure=not settings.DEBUG,
    )
    return token


def clear_storefront_customer_cookie(response, store):
    response.delete_cookie(customer_cookie_name(store), path=f"{shop_url(store)}/")


def _restore_customer_cookie(response, request, store):
    """A shopper signed in only by the link's token gets the 30-day cookie back, so their next visit
    (no token in the address) is still signed in. A top-level GET's cookie is kept even from the sandbox."""
    user = getattr(request, "_customer_cookie_user", None)
    if user is not None:
        set_storefront_customer_cookie(response, store, user)


_VISIT_BEACON = re.compile(r"<script data-visit-beacon\b.*?</script>", re.S)


def _without_visit_beacon(html):
    """The page without the layout's visit-tracking script, which carries the cart id but is not the cart."""
    return _VISIT_BEACON.sub("", html)


def _give_cart_cookie(response, request, store, html, markers=("data-cart-session=",)):
    """Give the browser its cart id, but only for a page that has the cart (see cart.py): `markers` tell.

    A new id on a page without the cart is not kept, so the visit-tracking script is given none (it would be a
    different "visitor" on every load): the id is taken out of `response`, and if the browser's storage is blocked
    too, the script skips the visit.
    """
    pending = getattr(request, "_cart_session", None)
    if not (pending and pending[1]):
        return
    page = _without_visit_beacon(html)
    if any(marker in page for marker in markers):
        response.set_cookie(
            cart_data.cookie_name(store), pending[0], max_age=cart_data.CART_COOKIE_AGE,
            path=f"{shop_url(store)}/", httponly=True, samesite="Lax", secure=not settings.DEBUG,
        )
    else:
        response.content = html.replace(f'data-visit-cart="{pending[0]}"', 'data-visit-cart=""', 1)


def _storefront_response(html, status=200, request=None, store=None):
    if isinstance(html, (HttpResponse, JsonResponse)):
        return html
    if request is not None and store is not None:
        visit = _admin_visit(request, store)
        if visit is not None and not visit.get("h") and request.headers.get("Sec-Fetch-Dest") != "iframe":
            html = _with_admin_bar(html, store, visit)
    if request is not None and store is not None:
        html = privacy.inject(html, store, request)
    response = HttpResponse(html, status=status)
    response["Content-Security-Policy"] = (
        "sandbox allow-scripts allow-forms "
        "allow-popups allow-popups-to-escape-sandbox"
    )
    response["Cache-Control"] = "no-store"
    if request is not None and store is not None:
        _restore_customer_cookie(response, request, store)
        _give_cart_cookie(response, request, store, html, markers=("data-cart-session=", "cart_session"))
    return response


@xframe_options_exempt
@csrf_exempt
def storefront_login(request, store_slug):
    """Storefront Customer Login: handles GET (login form) and POST (sign-in)."""
    store = get_object_or_404(Store, store_slug=store_slug)
    base = shop_url(store)
    return_to = request.GET.get("return_to") or request.POST.get("return_to") or f"{base}/account"
    if not return_to.startswith(base) and not return_to.startswith("/"):
        return_to = f"{base}/account"
    elif return_to.startswith("/") and not return_to.startswith(base):
        return_to = f"{base}{return_to}"

    customer, user = get_storefront_customer(store, request)
    if customer is not None and request.method == "GET":
        return _account_redirect(store, request, user)

    error = None
    if request.method == "POST":
        email = (request.POST.get("email") or "").strip().lower()
        password = request.POST.get("password") or ""

        if not email or not password:
            error = "Email and password are required."
        else:
            user = authenticate(request, username=email, password=password)
            if user is None:
                error = "Invalid email or password."
            else:
                customer = Customer.objects.filter(store=store, email__iexact=user.email).first()
                if customer is None:
                    try:
                        customer = Customer.objects.create(
                            store=store,
                            email=user.email,
                            first_name=user.first_name,
                            last_name=user.last_name,
                            email_subscription_status="not_subscribed",
                        )
                    except IntegrityError:
                        customer = Customer.objects.filter(store=store, email__iexact=user.email).first()
                # Link cart if open
                pending = getattr(request, "_cart_session", None)
                cart_id = pending[0] if pending else request.COOKIES.get(cart_data.cookie_name(store))
                if cart_id:
                    open_c = cart_data.open_cart(store, cart_id)
                    if open_c and not open_c.customer:
                        open_c.customer = customer
                        open_c.save(update_fields=["customer", "updated_at"])

                response = HttpResponseRedirect(signed_in_url(store, user, return_to))
                set_storefront_customer_cookie(response, store, user)
                return response

    html = render_page(
        store, "login", request=request, customer_page="login",
        customer=customer, customer_user=user, auth_error=error
    )
    return _storefront_response(html, status=400 if error else 200, request=request, store=store)


@xframe_options_exempt
@csrf_exempt
def storefront_register(request, store_slug):
    """Storefront Customer Registration: handles GET (signup form) and POST (account creation)."""
    store = get_object_or_404(Store, store_slug=store_slug)
    base = shop_url(store)
    return_to = request.GET.get("return_to") or request.POST.get("return_to") or f"{base}/account"
    if not return_to.startswith(base) and not return_to.startswith("/"):
        return_to = f"{base}/account"
    elif return_to.startswith("/") and not return_to.startswith(base):
        return_to = f"{base}{return_to}"

    customer, user = get_storefront_customer(store, request)
    if customer is not None and request.method == "GET":
        return _account_redirect(store, request, user)

    error = None
    if request.method == "POST":
        first_name = (request.POST.get("first_name") or "").strip()
        last_name = (request.POST.get("last_name") or "").strip()
        email = (request.POST.get("email") or "").strip().lower()
        phone = (request.POST.get("phone") or "").strip()
        password = request.POST.get("password") or ""
        confirm_password = request.POST.get("confirm_password") or ""
        newsletter = bool(request.POST.get("newsletter"))

        if not first_name or not last_name:
            error = "First name and last name are required."
        elif not email or "@" not in email:
            error = "Please enter a valid email address."
        elif len(password) < 8:
            error = "Password must be at least 8 characters long."
        elif password != confirm_password:
            error = "Passwords do not match."
        else:
            User = get_user_model()
            existing_user = User.objects.filter(email__iexact=email).first()
            if existing_user:
                if existing_user.check_password(password):
                    user = existing_user
                    customer = Customer.objects.filter(store=store, email__iexact=email).first()
                    if customer is None:
                        try:
                            customer = Customer.objects.create(
                                store=store,
                                email=email,
                                first_name=user.first_name or first_name,
                                last_name=user.last_name or last_name,
                                email_subscription_status="not_subscribed",
                            )
                        except IntegrityError:
                            customer = Customer.objects.filter(store=store, email__iexact=email).first()
                    if newsletter and customer is not None and customer.email_subscription_status != "subscribed":
                        privacy.record_consent(store, customer, request, email=True)
                    # Link cart if open
                    pending = getattr(request, "_cart_session", None)
                    cart_id = pending[0] if pending else request.COOKIES.get(cart_data.cookie_name(store))
                    if cart_id:
                        open_c = cart_data.open_cart(store, cart_id)
                        if open_c and not open_c.customer:
                            open_c.customer = customer
                            open_c.save(update_fields=["customer", "updated_at"])

                    response = HttpResponseRedirect(signed_in_url(store, user, return_to))
                    set_storefront_customer_cookie(response, store, user)
                    return response
                else:
                    error = "An account with this email already exists."
            else:
                try:
                    user = User.objects.create_user(
                        email=email,
                        password=password,
                        first_name=first_name,
                        last_name=last_name,
                        phone=phone,
                    )
                    customer = Customer.objects.filter(store=store, email__iexact=email).first()
                    if customer is None:
                        try:
                            customer = Customer.objects.create(
                                store=store,
                                email=email,
                                first_name=first_name,
                                last_name=last_name,
                                email_subscription_status="not_subscribed",
                            )
                        except IntegrityError:
                            customer = Customer.objects.filter(store=store, email__iexact=email).first()
                    if newsletter and customer is not None and customer.email_subscription_status != "subscribed":
                        privacy.record_consent(store, customer, request, email=True)
                    # Link cart if open
                    pending = getattr(request, "_cart_session", None)
                    cart_id = pending[0] if pending else request.COOKIES.get(cart_data.cookie_name(store))
                    if cart_id:
                        open_c = cart_data.open_cart(store, cart_id)
                        if open_c and not open_c.customer:
                            open_c.customer = customer
                            open_c.save(update_fields=["customer", "updated_at"])

                    response = HttpResponseRedirect(signed_in_url(store, user, return_to))
                    set_storefront_customer_cookie(response, store, user)
                    return response
                except Exception as e:
                    error = str(e) or "Unable to create account. Please try again."

    html = render_page(
        store, "register", request=request, customer_page="register",
        customer=customer, customer_user=user, auth_error=error
    )
    return _storefront_response(html, status=400 if error else 200, request=request, store=store)


@xframe_options_exempt
@csrf_exempt
def storefront_account(request, store_slug):
    """Storefront Customer Account Dashboard: shows orders, profile, and addresses."""
    store = get_object_or_404(Store, store_slug=store_slug)
    base = shop_url(store)
    customer, user = get_storefront_customer(store, request)

    if customer is None:
        login_url = f"{base}/account/login?return_to={quote(request.get_full_path())}"
        return HttpResponseRedirect(login_url)

    html = render_page(
        store, "account", request=request, customer_page="account",
        customer=customer, customer_user=user,
        auth_success=ACCOUNT_NOTICES.get(request.GET.get("notice", "")),
        auth_error=ACCOUNT_ERRORS.get(request.GET.get("error", "")),
    )
    return _storefront_response(html, request=request, store=store)


@xframe_options_exempt
@csrf_exempt
def storefront_logout(request, store_slug):
    """Storefront Customer Sign out: clears customer session cookie and redirects."""
    store = get_object_or_404(Store, store_slug=store_slug)
    base = shop_url(store)
    response = HttpResponseRedirect(f"{base}/account/login")
    clear_storefront_customer_cookie(response, store)
    return response


@xframe_options_exempt
@csrf_exempt
def storefront_account_profile(request, store_slug):
    """Storefront Customer Profile update: saves first_name, last_name, and phone."""
    store = get_object_or_404(Store, store_slug=store_slug)
    base = shop_url(store)
    customer, user = get_storefront_customer(store, request)
    if customer is None:
        return HttpResponseRedirect(f"{base}/account/login")

    if request.method == "POST":
        first_name = (request.POST.get("first_name") or "").strip()
        last_name = (request.POST.get("last_name") or "").strip()
        phone = (request.POST.get("phone") or "").strip()
        if phone and not MOBILE_NUMBER.fullmatch(phone):
            return _account_redirect(store, request, user, "phone_invalid")
        if first_name and last_name:
            customer.first_name = first_name
            customer.last_name = last_name
            customer.save(update_fields=["first_name", "last_name", "updated_at"])
            if user:
                user.first_name = first_name
                user.last_name = last_name
                user.phone = phone
                user.save(update_fields=["first_name", "last_name", "phone", "updated_at"])
            return _account_redirect(store, request, user, "profile_saved")

    return _account_redirect(store, request, user)


def _customer_address(customer, raw_id):
    """One of this customer's addresses by id, or None (unknown, someone else's, or not an id)."""
    try:
        return CustomerAddress.objects.filter(customer=customer, pk=raw_id).first()
    except (ValueError, DjangoValidationError):
        return None


def _posted_address(post):
    return {
        "first_name": (post.get("first_name") or "").strip(),
        "last_name": (post.get("last_name") or "").strip(),
        "company": (post.get("company") or "").strip(),
        "address_line_1": (post.get("address_line_1") or "").strip(),
        "address_line_2": (post.get("address_line_2") or "").strip(),
        "city": (post.get("city") or "").strip(),
        "state": (post.get("state") or "").strip(),
        "postal_code": (post.get("postal_code") or "").strip(),
        "country": (post.get("country") or "Australia").strip(),
        "phone": (post.get("phone") or "").strip(),
    }


_ADDRESS_KEY = ("address_line_1", "address_line_2", "city", "state", "postal_code", "country")


def _duplicate_address(customer, fields, exclude=None):
    """The customer's other address with the same street, city, state, postcode and country, if any."""
    wanted = tuple(fields[k] for k in _ADDRESS_KEY)
    for existing in CustomerAddress.objects.filter(customer=customer).exclude(pk=getattr(exclude, "pk", None)):
        if _same_address(wanted, tuple(getattr(existing, k) for k in _ADDRESS_KEY)):
            return existing
    return None


@xframe_options_exempt
@csrf_exempt
def storefront_account_address(request, store_slug):
    """Storefront Customer Address action: add, edit, delete, or set default address."""
    store = get_object_or_404(Store, store_slug=store_slug)
    base = shop_url(store)
    customer, user = get_storefront_customer(store, request)
    if customer is None:
        return HttpResponseRedirect(f"{base}/account/login")

    if request.method == "POST":
        action = request.POST.get("action", "add")
        fields = _posted_address(request.POST)
        is_default = bool(request.POST.get("is_default"))
        complete = all(fields[k] for k in ("address_line_1", "city", "state", "postal_code"))
        notice = None

        if action == "add" and complete:
            duplicate = _duplicate_address(customer, fields)
            if duplicate is not None:
                # Saving the same address twice (a double click) does not add it again
                if is_default and not duplicate.is_default:
                    customer_services.set_default_address(duplicate)
                notice = "address_exists"
            else:
                addr = CustomerAddress.objects.create(customer=customer, address_type="shipping", is_default=is_default, **fields)
                if is_default:
                    customer_services.set_default_address(addr)
                elif CustomerAddress.objects.filter(customer=customer).count() == 1:
                    addr.is_default = True
                    addr.save(update_fields=["is_default", "updated_at"])
                notice = "address_added"

        elif action == "edit" and complete:
            addr = _customer_address(customer, request.POST.get("address_id"))
            if addr is not None:
                if _duplicate_address(customer, fields, exclude=addr) is not None:
                    notice = "address_exists"
                else:
                    for key, value in fields.items():
                        setattr(addr, key, value)
                    addr.save()
                    if is_default and not addr.is_default:
                        customer_services.set_default_address(addr)
                    notice = "address_updated"

        elif action == "delete":
            addr = _customer_address(customer, request.POST.get("address_id"))
            if addr is not None:
                customer_services.delete_address(addr)
                notice = "address_deleted"

        elif action == "set_default":
            addr = _customer_address(customer, request.POST.get("address_id"))
            if addr is not None:
                customer_services.set_default_address(addr)
                notice = "address_default"

        _sync_customer_location(customer)
        return _account_redirect(store, request, user, notice)

    return _account_redirect(store, request, user)