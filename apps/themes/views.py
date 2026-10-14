import json
import re
import time
import uuid
from urllib.parse import urlencode

from django.conf import settings
from django.core import signing
from django.core.files.storage import default_storage
from django.db import connection
from django.db.models import F
from django.http import Http404, HttpResponse, HttpResponseRedirect, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.http import require_GET

from liquid.exceptions import LiquidError
from markupsafe import escape

from rest_framework import serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.renderers import BaseRenderer, JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.catalog.services import published_collections, published_products
from apps.catalog.views import MAX_IMAGE_SIZE, image_extension
from apps.core.permissions import HasStorePermission, IsStoreMember
from django.contrib.auth import get_user_model

from apps.tenancy.models import Store, StoreMembership

from .models import BlogPost, ThemeImage
from .customizer import read_settings, save_settings, validate_settings
from . import section_sync as sync
from .rendering import published_posts, render_page, render_synced_section_fast, shop_url
from .rendering import _collect_fonts, google_fonts_link
from .schemas import PAGES, public_schema, resolve, validate_custom_data
from .services import (
    delete_path,
    generate_css_variables,
    get_store_theme,
    rename_path,
    restore_section,
    save_file,
    save_section_values,
    synced_files,
    synced_section,
    synced_sections,
    templates_changed,
    code_files,
)


class StoreThemeView(APIView):
    permission_classes = [IsAuthenticated, IsStoreMember, HasStorePermission]
    permission_resource = "themes"

    def store_theme(self):
        return get_store_theme(self.request.store)


class ThemeFilesView(StoreThemeView):
    """
    GET    /api/v1/themes/files/            all files of the store's theme
    POST   /api/v1/themes/files/            create or save {path, content}
    DELETE /api/v1/themes/files/?path=...   delete a file or a folder
    """

    def get(self, request):
        files = code_files(self.store_theme())

        return Response({
            "store": str(request.store.store_id),
            "storefrontUrl": request.build_absolute_uri(
                shop_url(request.store) + "/"
            ),
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
        merged = resolve(store_theme.custom_data)

        if body.get("settings") is not None:
            if not isinstance(body["settings"], dict):
                raise ValidationError({"settings": "Expected an object."})
            merged["settings"].update(body["settings"])

        templates = body.get("templates") or {}
        if not isinstance(templates, dict):
            raise ValidationError({"templates": "Expected an object."})

        for page, changes in templates.items():
            if page not in PAGES or not isinstance(changes, dict):
                raise ValidationError({"templates": f"Unknown page: {page}."})
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
    Banner, slide and image-block pictures: JPG, PNG or WEBP up to 5MB,
    checked by their first bytes (same rules as product photos).
    """

    parser_classes = [MultiPartParser]

    def post(self, request):
        file = request.FILES.get("image")
        if file is None:
            raise ValidationError({"image": "Choose an image to upload."})
        if file.size > MAX_IMAGE_SIZE:
            raise ValidationError({"image": "Image is too large. Maximum size is 5MB."})

        ext = image_extension(file.read(12))
        if ext is None:
            raise ValidationError({"image": "Not a supported image. Use JPG, PNG or WEBP."})
        file.seek(0)

        ctype = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[ext]
        image = ThemeImage.objects.create(
            store_id=request.store.store_id, content_type=ctype, data=file.read(), size=file.size,
        )
        url = request.build_absolute_uri(f"/s/_img/{image.id}")
        return Response({"url": url}, status=status.HTTP_201_CREATED)


@require_GET
def theme_image(request, image_id):
    """Public: a customizer picture, straight from the database."""
    image = get_object_or_404(ThemeImage, pk=image_id)
    response = HttpResponse(bytes(image.data), content_type=image.content_type)
    response["Cache-Control"] = "public, max-age=31536000, immutable"
    response["Cross-Origin-Resource-Policy"] = "cross-origin"
    return response


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
    var a = e.target.closest && e.target.closest("a[href]");
    if (!a || (a.target && a.target !== "_self")) return null;
    var url;
    try { url = new URL(a.href, document.baseURI); } catch (err) { return null; }
    if (url.origin !== new URL(document.baseURI).origin) return null;
    var m = url.pathname.match(/^\\/s\\/[^/]+(\\/.*)?$/);
    if (!m) return null;
    var path = (m[1] || "/").replace(/\\/+$/, "") || "/";
    return /^\\/(products\\/[0-9a-f-]{36}|collections(\\/[^/]+)?|blog(\\/[^/]+){0,2})?$/i.test(path) ? path : null;
  }

  // Edit mode: a click selects the section or block under it, and does nothing else -
  // except on a product or blog post link, which opens that page (as does any store link with Edit mode off).
  // The customizer opens the page's template itself, so the preview stays editable.
  document.addEventListener("click", function (e) {
    var page = storePage(e);
    if (page && (!editing || /^\\/(products|blog)\\/[^/]+$/.test(page))) {
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
  // Edit mode: right-click opens the customizer's menu (Edit code) for the section or block under it
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


def _preview_target(request):
    """
    (page, render_page arguments, message) for ?page=index|product|collection|blog|article.
    The product page shows ?product=<id> or the newest product; the collection page shows all products;
    the article page shows ?article=<handle> or the newest post.
    message: why the page can't be shown (no product to show), else None.
    """
    page = request.query_params.get("page") or "index"
    if page not in PAGES:
        raise ValidationError({"page": f"page must be one of: {', '.join(PAGES)}."})
    if page == "collection":
        return page, {"collection": {"name": "All products", "obj": None}}, None
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
        clean = validate_custom_data(request.data)
        page, target, message = _preview_target(request)
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
                html = render_page(request.store, page, custom_data=clean, editor=True, only_section=section_id, **target)
            except LiquidError as error:
                html = f"<pre style='padding:24px;color:#b42318'>Liquid error: {escape(str(error))}</pre>"
            return Response({"html": html, "fonts": google_fonts_link(_collect_fonts(resolve(clean)))})

        try:
            html = render_page(request.store, page, custom_data=clean, editor=True, **target)
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


class ThemeSchemaView(APIView):
    """
    GET /api/v1/themes/schema/

    Returns every section type and its settings.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(
            public_schema()
        )


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
        token = signing.dumps(
            {
                "u": str(request.user.pk),
                "s": str(request.store.store_id),
                "r": _safe_return_url(request.data.get("return_url")),
            },
            salt=ADMIN_BAR_SALT,
        )
        url = request.build_absolute_uri(shop_url(request.store) + path)
        return Response({"url": url + ("&" if "?" in url else "?") + urlencode({"admin_bar": token})})


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

        try:
            html = render(
                store,
                request.GET,
                **kwargs,
            )

            status_code = 200

        except Http404:
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

        # Store members who came from "View store" get the "Back to admin" bar.
        # Never inside a frame (the customizer's live preview).
        visit = _admin_visit(request, store)
        if visit is not None and not visit.get("h") and request.headers.get("Sec-Fetch-Dest") != "iframe":
            html = _with_admin_bar(html, store, visit)

        response = HttpResponse(
            html,
            status=status_code,
        )

        response["Content-Security-Policy"] = (
            "sandbox allow-scripts allow-forms "
            "allow-popups allow-popups-to-escape-sandbox"
        )

        response["Cache-Control"] = "no-store"

        return response

    return view


@_storefront
def storefront_home(store, params):
    return render_page(
        store,
        "index",
    )


@_storefront
def storefront_product(
    store,
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
    )


def _listing(params):
    """?sort=newest|price-asc|price-desc&page=N"""
    try:
        page_number = int(params.get("page", 1))
    except ValueError:
        page_number = 1
    return {"sort": params.get("sort", "newest"), "page_number": page_number}


@_storefront
def storefront_collection(
    store,
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
            **_listing(params),
        )

    # Draft collections, and ones off the Online Store, are not found
    lookup = {"pk": collection_id} if collection_id is not None else {"handle": handle}
    collection = get_object_or_404(published_collections(store), **lookup)

    return render_page(
        store,
        "collection",
        collection={
            "name": collection.name,
            "obj": collection,
        },
        **_listing(params),
    )

BLOG_FILTERS = ("category", "tagged", "author")


@_storefront
def storefront_blog(store, params, kind=None, handle=None):
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
    return render_page(store, "blog", blog_filter=blog_filter, page_number=page_number)


@_storefront
def storefront_article(store, params, handle):
    """/blog/<handle>: one published post; drafts and unknown handles are not found."""
    if not published_posts(store).filter(handle=handle).exists():
        raise Http404("Post not found.")
    return render_page(store, "article", article=handle)
