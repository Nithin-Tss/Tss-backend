from django.http import HttpResponse
from django.utils.decorators import method_decorator
from django.views.decorators.clickjacking import xframe_options_exempt
from liquid import Environment, DictLoader
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .services import ThemePathError


def _context(request):
    store = services.resolve_store(request)
    if store is None:
        return None, None, Response(
            {"detail": "Store not found."}, status=status.HTTP_404_NOT_FOUND
        )
    return store, services.get_store_theme(store), None


def _bad(exc):
    return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class ThemeFilesView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        store, theme, err = _context(request)
        if err:
            return err
        files = theme.files.order_by("file_path").values("file_path", "content")
        return Response(
            {
                "store": str(store.pk),
                "files": [{"path": f["file_path"], "content": f["content"]} for f in files],
            }
        )

    def post(self, request):
        store, theme, err = _context(request)
        if err:
            return err
        try:
            f = services.save_file(theme, request.data.get("path"), request.data.get("content"))
        except ThemePathError as e:
            return _bad(e)
        return Response({"path": f.file_path, "updated_at": f.updated_at})

    def delete(self, request):
        store, theme, err = _context(request)
        if err:
            return err
        try:
            services.delete_path(theme, request.query_params.get("path"))
        except ThemePathError as e:
            return _bad(e)
        return Response({"deleted": True})


class ThemeRenameView(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        store, theme, err = _context(request)
        if err:
            return err
        try:
            services.rename_path(theme, request.data.get("source"), request.data.get("target"))
        except ThemePathError as e:
            return _bad(e)
        return Response({"renamed": True})


@method_decorator(xframe_options_exempt, name="dispatch")
class ThemeRenderView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        store, theme, err = _context(request)
        if err:
            return HttpResponse("Store not found.", status=404)
        templates = {
            f.file_path[: -len(".liquid")]: f.content
            for f in theme.files.all()
            if f.file_path.endswith(".liquid")
        }
        env = Environment(loader=DictLoader(templates))
        try:
            entry = "layout/theme" if "layout/theme" in templates else "index"
            html = env.get_template(entry).render(store={"name": store.store_name})
        except Exception as e:
            return HttpResponse(
                f"<pre style='color:#b91c1c;font:14px monospace'>Liquid error: {e}</pre>",
                status=200,
            )
        return HttpResponse(html)
