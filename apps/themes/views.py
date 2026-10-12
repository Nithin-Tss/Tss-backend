import json
import re
import uuid
from urllib.parse import urlencode

from django.conf import settings
from django.core import signing
from django.core.files.storage import default_storage
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.http import require_GET

from liquid.exceptions import LiquidError
from markupsafe import escape

from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.catalog.models import Collection
from apps.catalog.services import published_products
from apps.catalog.views import MAX_IMAGE_SIZE, image_extension
from apps.core.permissions import IsStoreMember
from apps.tenancy.models import Store, StoreMembership

from .customizer import read_settings, save_settings, validate_settings
from .rendering import render_page, shop_url
from .rendering import _collect_fonts, google_fonts_link
from .schemas import PAGES, public_schema, resolve, validate_custom_data
from .services import (
    delete_path,
    generate_css_variables,
    get_store_theme,
    rename_path,
    save_file,
    theme_files,
)


class StoreThemeView(APIView):
    permission_classes = [IsAuthenticated, IsStoreMember]

    def store_theme(self):
        return get_store_theme(self.request.store)


class ThemeFilesView(StoreThemeView):
    """
    GET    /api/v1/themes/files/            all files of the store's theme
    POST   /api/v1/themes/files/            create or save {path, content}
    DELETE /api/v1/themes/files/?path=...   delete a file or a folder
    """

    def get(self, request):
        files = theme_files(self.store_theme())

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
        )

        return Response({
            "path": saved.file_path,
            "size": saved.file_size,
        })

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

        store_theme.custom_data = validate_custom_data(merged)
        store_theme.save(update_fields=["custom_data", "updated_at"])
        return Response({"saved": True})

    def put(self, request):
        store_theme = self.store_theme()

        store_theme.custom_data = validate_custom_data(
            request.data
        )

        store_theme.save(
            update_fields=[
                "custom_data",
                "updated_at",
            ]
        )

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

        name = default_storage.save(f"themes/{request.store.store_id}/{uuid.uuid4().hex}.{ext}", file)
        url = request.build_absolute_uri(settings.MEDIA_URL + name)
        return Response({"url": url}, status=status.HTTP_201_CREATED)


# Added to the customizer preview only. Every section gets a drag handle; a drop
# tells the customizer where the section should go. The page has no access to
# the admin (sandboxed iframe), so it can only send these messages.
PREVIEW_EDITOR = """
<style>
[data-tss-section]{position:relative}
[data-tss-section]:hover{outline:2px dashed rgba(20,27,45,.35);outline-offset:-2px}
[data-tss-section].tss-selected{outline:2px solid #141b2d;outline-offset:-2px}
.tss-handle{position:absolute;top:10px;right:10px;z-index:50;display:none;align-items:center;gap:6px;
  padding:6px 10px;border:0;border-radius:8px;background:#141b2d;color:#fff;font:600 12px/1 system-ui,sans-serif;
  cursor:grab;touch-action:none;box-shadow:0 4px 12px rgba(20,27,45,.25)}
[data-tss-section]:hover>.tss-handle,.tss-selected>.tss-handle{display:inline-flex}
@media (hover:none){.tss-handle{display:inline-flex}}
.tss-dragging{opacity:.35}
.tss-drop-line{position:absolute;left:0;right:0;height:4px;margin-top:-2px;background:#141b2d;border-radius:4px;
  box-shadow:0 0 0 3px rgba(20,27,45,.15);z-index:60;pointer-events:none;display:none}
</style>
<script>
(function () {
  var config = __CONFIG__;
  var selected = config.selected;
  var pendingImages = {};
  function send(message) { message.source = "tss-preview"; parent.postMessage(message, "*"); }
  function all() { return [].slice.call(document.querySelectorAll("[data-tss-section]")); }
  function byId(id) { return all().filter(function (el) { return el.getAttribute("data-tss-section") === id; })[0]; }

  var line = document.createElement("div");
  line.className = "tss-drop-line";
  document.body.appendChild(line);

  function select(id) {
    selected = id;
    all().forEach(function (el) { el.classList.toggle("tss-selected", el.getAttribute("data-tss-section") === id); });
  }

  // Drag handle and click-to-select on a section
  function enhance(el) {
    if (el.__tssReady) return;
    el.__tssReady = true;
    var id = el.getAttribute("data-tss-section");
    var handle = document.createElement("button");
    handle.type = "button";
    handle.className = "tss-handle";
    handle.setAttribute("aria-label", "Drag to move this section");
    handle.innerHTML = "&#10303; Move";
    el.appendChild(handle);
    handle.addEventListener("pointerdown", function (e) { startDrag(e, el, id); });
    el.addEventListener("click", function (e) {
      if (e.target.closest(".tss-handle") || e.target.closest("a,button,input,select,textarea,video,iframe")) return;
      send({ type: "select", id: id });
    });
  }

  function startDrag(e, el, id) {
    e.preventDefault();
    var target = null;
    el.classList.add("tss-dragging");

    function move(ev) {
      target = null;
      var y = ev.clientY;
      var sections = all();
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
    var others = all().filter(function (x) { return x !== el; });
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
      select(selected);
    });
  }

  function reorder(ids) {
    var first = all()[0];
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
    else if (m.type === "select") select(m.id);
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

  send({ type: "ready" });
})();
</script>
"""


class ThemePreviewView(StoreThemeView):
    """
    POST /api/v1/themes/preview/?scroll=&selected=  {unsaved custom_data} -> {"html": ...}
    Renders the home page with the customizer's unsaved sections, for its
    live preview, with drag handles on every section. Nothing is saved.
    """

    def post(self, request):
        clean = validate_custom_data(request.data)

        # ?section=<id>: just that section, to swap into the open preview (plus the fonts it may need)
        section_id = request.query_params.get("section")
        if section_id:
            try:
                html = render_page(request.store, "index", custom_data=clean, editor=True, only_section=section_id)
            except LiquidError as error:
                html = f"<pre style='padding:24px;color:#b42318'>Liquid error: {escape(str(error))}</pre>"
            return Response({"html": html, "fonts": google_fonts_link(_collect_fonts(resolve(clean)))})

        try:
            html = render_page(request.store, "index", custom_data=clean, editor=True)
        except LiquidError as error:
            html = f"<pre style='padding:24px;color:#b42318'>Liquid error: {escape(str(error))}</pre>"

        # Store links are site-relative (/s/<slug>/...); resolve them against the API host
        base = f'<base href="{escape(request.build_absolute_uri("/"))}" target="_blank">'
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

    permission_classes = [IsAuthenticated, IsStoreMember]

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
):
    if collection_id is None:
        return render_page(
            store,
            "collection",
            collection={
                "name": "All products",
                "obj": None,
            },
            **_listing(params),
        )

    collection = get_object_or_404(
        Collection,
        store=store,
        pk=collection_id,
    )

    return render_page(
        store,
        "collection",
        collection={
            "name": collection.name,
            "obj": collection,
        },
        **_listing(params),
    )