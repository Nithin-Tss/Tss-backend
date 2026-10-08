import html
import re
import uuid

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.utils.decorators import method_decorator
from django.views.decorators.clickjacking import xframe_options_exempt
from liquid import DictLoader, Environment
from liquid.exceptions import LiquidError
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tenancy.models import Store

from .models import ThemeFile
from .serializers import (
    ThemeFileRenameSerializer,
    ThemeFileWriteSerializer,
    validate_path,
)

LAYOUT_PATH = "layout/theme.liquid"
TEMPLATE_PATH = "templates/index.liquid"

STARTER_FILES = {
    LAYOUT_PATH: (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "  <head>\n"
        '    <meta charset="utf-8">\n'
        '    <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "    <title>{{ page_title }}</title>\n"
        "    <style>\n"
        "      body { margin: 0; font-family: system-ui, sans-serif; }\n"
        "    </style>\n"
        "  </head>\n"
        "  <body>\n"
        "    {{ content_for_layout }}\n"
        "  </body>\n"
        "</html>\n"
    ),
    TEMPLATE_PATH: (
        "{% assign title = 'Welcome to ' | append: shop.name %}\n"
        "{% render 'hero', heading: title %}\n"
        "\n"
        "<main>\n"
        "  {% for item in (1..3) %}\n"
        "    <p>Featured item {{ item }}</p>\n"
        "  {% endfor %}\n"
        "</main>\n"
    ),
    "sections/hero.liquid": (
        "<style>\n"
        "  .hero { padding: 96px 24px; text-align: center; background: #161c2c; color: #fff; }\n"
        "</style>\n"
        '<section class="hero">\n'
        "  <h1>{{ heading | default: shop.name }}</h1>\n"
        "  {% render 'button', label: 'Shop now' %}\n"
        "</section>\n"
    ),
    "snippets/button.liquid": (
        '<button style="margin-top:16px;padding:12px 28px;border:0;border-radius:12px;cursor:pointer">\n'
        "  {{ label }}\n"
        "</button>\n"
    ),
}


def resolve_store(request):
    """
    Resolve the store the request is about.

    TODO: once login issues tokens, derive the store from the authenticated
    user's StoreMembership instead of trusting the header / query string.
    """
    raw = request.headers.get("X-Store-Id") or request.query_params.get("store")

    if raw:
        try:
            store_id = uuid.UUID(raw)
        except ValueError:
            raise ValidationError({"detail": "Invalid store id."})

        try:
            return Store.objects.get(pk=store_id)
        except Store.DoesNotExist:
            raise NotFound("Store not found.")

    if settings.DEBUG:
        store, _ = Store.objects.get_or_create(
            store_slug="default",
            defaults={"store_name": "Default Store", "status": "active"},
        )
        return store

    raise ValidationError({"detail": "X-Store-Id header is required."})


def seed_if_empty(store):
    if ThemeFile.objects.filter(store=store).exists():
        return

    ThemeFile.objects.bulk_create(
        [ThemeFile(store=store, path=p, content=c) for p, c in STARTER_FILES.items()]
    )


def serialize(file):
    return {
        "id": str(file.id),
        "path": file.path,
        "content": file.content,
        "updatedAt": file.updated_at.isoformat(),
    }


def validate_path_param(value):
    try:
        return validate_path(value)
    except serializers.ValidationError as exc:
        raise ValidationError({"path": exc.detail})


def folder_regex(path):
    return f"^{re.escape(path)}(/.*)?$"


class ThemeFilesView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        store = resolve_store(request)
        seed_if_empty(store)
        files = ThemeFile.objects.filter(store=store)
        return Response({"store": str(store.store_id), "files": [serialize(f) for f in files]})

    def post(self, request):
        store = resolve_store(request)
        serializer = ThemeFileWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        file, created = ThemeFile.objects.update_or_create(
            store=store,
            path=serializer.validated_data["path"],
            defaults={"content": serializer.validated_data["content"]},
        )
        return Response(
            serialize(file),
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    def delete(self, request):
        store = resolve_store(request)
        path = validate_path_param(request.query_params.get("path", ""))

        deleted, _ = ThemeFile.objects.filter(
            store=store, path__regex=folder_regex(path)
        ).delete()
        return Response({"deleted": deleted})


class ThemeFileRenameView(APIView):
    authentication_classes = []
    permission_classes = []

    @transaction.atomic
    def post(self, request):
        store = resolve_store(request)
        serializer = ThemeFileRenameSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        source = serializer.validated_data["source"]
        target = serializer.validated_data["target"]

        if target == source or target.startswith(source + "/"):
            raise ValidationError({"target": "Cannot move an item into itself."})

        if source.endswith(".liquid") and not target.endswith(".liquid"):
            raise ValidationError({"target": "Theme files must keep the .liquid extension."})

        moving = list(
            ThemeFile.objects.select_for_update().filter(
                store=store, path__regex=folder_regex(source)
            )
        )
        if not moving:
            raise NotFound("Nothing found at that path.")

        new_paths = [target + f.path[len(source):] for f in moving]
        clash = (
            ThemeFile.objects.filter(store=store, path__in=new_paths)
            .exclude(pk__in=[f.pk for f in moving])
            .exists()
        )
        if clash:
            raise ValidationError({"target": "An item with that name already exists."})

        for f, new_path in zip(moving, new_paths):
            f.path = new_path
            f.save(update_fields=["path", "updated_at"])

        return Response({"moved": len(moving)})


def message_page(title, detail):
    return (
        "<!doctype html><meta charset='utf-8'>"
        "<body style='font-family:system-ui;padding:24px;color:#161c2c'>"
        f"<h3 style='margin-top:0'>{html.escape(title)}</h3>"
        f"<pre style='white-space:pre-wrap;background:#f8f8f8;padding:12px;border-radius:8px'>{html.escape(detail)}</pre>"
        "</body>"
    )


def render_theme(files, store):
    """
    Render templates/index.liquid inside layout/theme.liquid.

    {% render 'name' %} / {% include 'name' %} resolve 'name' to
    snippets/name.liquid first, then sections/name.liquid, then a full path.
    """
    sources = {p: c for p, c in files.items() if p.endswith(".liquid")}

    for folder in ("sections", "snippets"):  # snippets win on a name clash
        for path, content in sources.copy().items():
            head, _, name = path.rpartition("/")
            if head == folder:
                sources[name[: -len(".liquid")]] = content

    if TEMPLATE_PATH not in sources:
        return message_page("Nothing to render", f"Create {TEMPLATE_PATH} to see your theme here.")

    env = Environment(loader=DictLoader(sources))
    context = {"shop": {"name": store.store_name}, "page_title": store.store_name}

    try:
        content = env.get_template(TEMPLATE_PATH).render(**context)

        if LAYOUT_PATH in sources:
            return env.get_template(LAYOUT_PATH).render(content_for_layout=content, **context)

        return content
    except LiquidError as exc:
        return message_page("Liquid error", str(exc))


@method_decorator(xframe_options_exempt, name="dispatch")
class ThemeRenderView(APIView):
    """Public: returns the store's rendered theme as an HTML document for the preview iframe."""

    authentication_classes = []
    permission_classes = []

    def get(self, request):
        store = resolve_store(request)
        seed_if_empty(store)
        files = {f.path: f.content for f in ThemeFile.objects.filter(store=store)}

        response = HttpResponse(render_theme(files, store), content_type="text/html; charset=utf-8")
        # Theme code is written by users: never let it run with our origin's privileges.
        response["Content-Security-Policy"] = "sandbox allow-scripts allow-forms allow-popups"
        response["Cache-Control"] = "no-store"
        return response
