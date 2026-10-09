import json
import uuid

from django.conf import settings
from django.core.files.storage import default_storage
from django.http import Http404, HttpResponse
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

from apps.catalog.models import Collection, Product
from apps.catalog.services import ACTIVE
from apps.catalog.views import MAX_IMAGE_SIZE, image_extension
from apps.core.permissions import IsStoreMember
from apps.tenancy.models import Store

from .customizer import read_settings, save_settings, validate_settings
from .rendering import render_page, shop_url
from .schemas import public_schema, resolve, validate_custom_data
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
  function send(message) { message.source = "tss-preview"; parent.postMessage(message, "*"); }

  var sections = [].slice.call(document.querySelectorAll("[data-tss-section]"));
  var line = document.createElement("div");
  line.className = "tss-drop-line";
  document.body.appendChild(line);

  sections.forEach(function (el) {
    var id = el.getAttribute("data-tss-section");
    if (id === config.selected) el.classList.add("tss-selected");

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
  });

  function startDrag(e, el, id) {
    e.preventDefault();
    var target = null;
    el.classList.add("tss-dragging");

    function move(ev) {
      target = null;
      var y = ev.clientY;
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
      // Scroll while dragging near the top or bottom edge
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

  // Keep the scroll position when the preview refreshes after an edit
  if (config.scroll) window.scrollTo(0, config.scroll);
  var timer = null;
  window.addEventListener("scroll", function () {
    clearTimeout(timer);
    timer = setTimeout(function () { send({ type: "scroll", y: Math.round(window.scrollY) }); }, 80);
  }, { passive: true });
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

        try:
            html = render(
                store,
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
def storefront_home(store):
    return render_page(
        store,
        "index",
    )


@_storefront
def storefront_product(
    store,
    product_id,
):
    product = (
        Product.objects
        .filter(
            store=store,
            pk=product_id,
            status=ACTIVE,
        )
        .prefetch_related(
            "images",
            "variants",
        )
        .first()
    )

    if product is None:
        raise Http404(
            "Product not found."
        )

    return render_page(
        store,
        "product",
        product=product,
    )


@_storefront
def storefront_collection(
    store,
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
    )