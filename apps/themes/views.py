from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.http import require_GET
from liquid.exceptions import LiquidError
from markupsafe import escape
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.catalog.models import Collection, Product
from apps.catalog.services import ACTIVE
from apps.core.permissions import IsStoreMember
from apps.tenancy.models import Store

from .rendering import render_page, shop_url
from .schemas import public_schema, resolve, validate_custom_data
from .services import (
    delete_path,
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
            "storefrontUrl": request.build_absolute_uri(shop_url(request.store) + "/"),
            "files": [{"path": path, "content": content} for path, content in files.items()],
        })

    def post(self, request):
        saved = save_file(
            self.store_theme(),
            request.data.get("path"),
            request.data.get("content", ""),
        )
        return Response({"path": saved.file_path, "size": saved.file_size})

    def delete(self, request):
        removed = delete_path(self.store_theme(), request.query_params.get("path"))
        return Response({"deleted": removed})


class ThemeRenameView(StoreThemeView):
    """POST /api/v1/themes/files/rename/  {source, target}"""

    def post(self, request):
        moves = rename_path(
            self.store_theme(),
            request.data.get("source"),
            request.data.get("target"),
        )
        return Response({"moved": moves})


class ThemeSettingsView(StoreThemeView):
    """
    GET /api/v1/themes/settings/   section layout and settings (with defaults)
    PUT /api/v1/themes/settings/   replace them; validated against the schema
    """

    def get(self, request):
        return Response(resolve(self.store_theme().custom_data))

    def put(self, request):
        store_theme = self.store_theme()
        store_theme.custom_data = validate_custom_data(request.data)
        store_theme.save(update_fields=["custom_data", "updated_at"])
        return Response(resolve(store_theme.custom_data), status=status.HTTP_200_OK)


class ThemeSchemaView(APIView):
    """GET /api/v1/themes/schema/  every section type and its settings."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(public_schema())


# ------------------------------------------------------------ storefront


def _storefront(render):
    """
    Wraps a public storefront view.

    Theme code is written by store owners, so the page is served with a CSP
    sandbox: it runs as an isolated origin and can't use this site's
    cookies or API. It may be framed, for the editor's live preview.
    """

    @xframe_options_exempt
    @require_GET
    def view(request, store_slug, **kwargs):
        store = get_object_or_404(Store, store_slug=store_slug)

        try:
            html = render(store, **kwargs)
            status_code = 200
        except LiquidError as error:
            html = (
                "<!doctype html><meta charset='utf-8'><title>Theme error</title>"
                "<pre style='font:14px/1.5 monospace;padding:24px;color:#b42318'>"
                f"Liquid error: {escape(str(error))}</pre>"
            )
            status_code = 500

        response = HttpResponse(html, status=status_code)
        response["Content-Security-Policy"] = (
            "sandbox allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox"
        )
        response["Cache-Control"] = "no-store"
        return response

    return view


@_storefront
def storefront_home(store):
    return render_page(store, "index")


@_storefront
def storefront_product(store, product_id):
    product = Product.objects.filter(store=store, pk=product_id, status=ACTIVE).prefetch_related(
        "images", "variants"
    ).first()

    if product is None:
        raise Http404("Product not found.")

    return render_page(store, "product", product=product)


@_storefront
def storefront_collection(store, collection_id=None):
    if collection_id is None:
        return render_page(store, "collection", collection={"name": "All products", "obj": None})

    collection = get_object_or_404(Collection, store=store, pk=collection_id)
    return render_page(store, "collection", collection={"name": collection.name, "obj": collection})
