"""
POST /api/v1/assistant/chat/   {"message": "...", "history": [{"role": "user"|"assistant", "content": "..."}]}
    -> {"answer": "...", "tools": [{"name", "arguments"}], "actions": [{"label", "path"}]}

Local only: answers 404 unless ASSISTANT_REAL_MODE is set (config/assistant_local_settings.py).
The chat itself writes nothing, not even an audit row. A change is only proposed ("proposal" in the reply); it is
saved when the owner clicks Apply (AssistantApplyView).
"""
import logging
import uuid

from django.conf import settings
from django.core.cache import cache
from django.http import Http404
from rest_framework import exceptions
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from apps.audit import services as audit
from apps.core.permissions import IsStoreMember
from apps.platform_admin.models import ApiKey
from apps.tenancy.access import membership_permissions

from apps.themes.services import save_file
from apps.themes.views import StoreThemeView

from . import actions, agent, queries, themecode, tools
from .model import ModelError

logger = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 500
MAX_HISTORY_ITEMS = 6
MAX_HISTORY_ITEM_CHARS = 1_000
MAX_HISTORY_TOTAL_CHARS = 4_000
HISTORY_ROLES = ("user", "assistant")
THEME_HEADER = "X-Theme-Id"


def editing_theme_id(request):
    """The theme the admin is editing, from X-Theme-Id; None unless it is a UUID. queries.theme_for() then
    accepts it only when it is a private theme of this store."""
    try:
        return str(uuid.UUID(request.headers.get(THEME_HEADER, "")))
    except ValueError:
        return None


def clean_history(raw):
    """
    The earlier turns of this chat, sent by the browser: [{"role": "user"|"assistant", "content": "..."}].
    Absent or null means none. Anything else that is not exactly that shape is refused, so the model only ever
    sees plain conversation text. It is context, never instructions, and carries no store, theme or tool data.
    """
    if raw is None:
        return []
    problem = {"history": "The earlier messages are not valid."}
    if not isinstance(raw, list) or len(raw) > MAX_HISTORY_ITEMS:
        raise exceptions.ValidationError(problem)
    cleaned, total = [], 0
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"role", "content"}:
            raise exceptions.ValidationError(problem)
        role, content = item["role"], item["content"]
        if role not in HISTORY_ROLES or not isinstance(content, str) or not content.strip():
            raise exceptions.ValidationError(problem)
        content = content.strip()
        total += len(content)
        if len(content) > MAX_HISTORY_ITEM_CHARS or total > MAX_HISTORY_TOTAL_CHARS:
            raise exceptions.ValidationError(problem)
        cleaned.append({"role": role, "content": content})
    return cleaned


LOCK_GRACE = 15  # seconds a lock outlives one model reply: short, and refreshed while the request is alive


def _lock_ttl():
    return int(settings.OPENROUTER_TIMEOUT) + LOCK_GRACE


def take_lock(lock, token):
    """Mark `token` as the person's current question, replacing any older one (that request is cancelled)."""
    cache.set(lock, token, timeout=_lock_ttl())


def keep_lock(lock, token):
    """True while `token` is still the current question; the lock's time is renewed. False once it was replaced."""
    if cache.get(lock) != token:
        return False
    cache.set(lock, token, timeout=_lock_ttl())
    return True


def release_lock(lock, token):
    """Free the lock, but only if it is still this request's: a newer question's lock is never deleted."""
    if cache.get(lock) == token:
        cache.delete(lock)


class NoApiKeys(BasePermission):
    """The assistant is for people signed in to the admin, not for API keys."""

    message = "The assistant can't be used with an API key."

    def has_permission(self, request, view):
        return not isinstance(request.auth, ApiKey)


class AssistantThrottle(SimpleRateThrottle):
    """Its own limit, per user: each answer costs a minute of this computer's CPU."""

    scope = "assistant"
    rate = "10/min"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": request.user.pk}


class AssistantChatView(APIView):
    permission_classes = [IsAuthenticated, NoApiKeys, IsStoreMember]
    throttle_classes = [AssistantThrottle]

    def initial(self, request, *args, **kwargs):
        if not getattr(settings, "ASSISTANT_REAL_MODE", False):
            raise Http404
        try:
            super().initial(request, *args, **kwargs)
        finally:
            # IsStoreMember registered this request for auditing, and AuditMiddleware would save a row for
            # a successful POST. The assistant writes nothing, so switch that off.
            request._request.audit = None
            audit.end()

    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        message = data.get("message")
        if not isinstance(message, str) or not message.strip():
            raise exceptions.ValidationError({"message": "Type a question."})
        message = message.strip()
        if len(message) > MAX_MESSAGE_CHARS:
            raise exceptions.ValidationError({"message": f"Keep the question under {MAX_MESSAGE_CHARS} characters."})
        history = clean_history(data.get("history"))

        # Data tools are offered (and run) only with their own permission; admin_help is for any member
        permissions = frozenset(membership_permissions(request.membership))
        reads_data = any(t.permission for t in tools.available_tools(permissions))

        if reads_data and not queries.alias_configured():
            return Response({"detail": "The assistant's read-only database is not set up."}, status=503)

        # The latest question per person wins: a new one takes the lock, and the old one (the browser stopped it)
        # stops at its next check and its result is dropped
        lock, token = f"assistant:busy:{request.user.pk}", uuid.uuid4().hex
        take_lock(lock, token)

        def still_current():
            return keep_lock(lock, token)

        try:
            if reads_data:  # a member who can only use the guide never touches the database
                queries.ensure_read_only()
            ctx = tools.Context(
                store_id=request.store.pk,  # from the signed-in member's store, never from the model
                timezone_name=request.store.timezone,
                permissions=permissions,
                theme_id=editing_theme_id(request),
            )
            answer, used = agent.run(message, ctx, history=history, still_current=still_current)
            if not still_current():  # replaced while the last model call was running: ignore this result
                raise agent.Superseded()
        except agent.Superseded:
            return Response({"detail": "A newer question replaced this one."}, status=409)
        except ModelError as error:
            return Response({"detail": str(error)}, status=503)
        except queries.ReadOnlyNotEnforced:
            logger.error("Assistant refused: the read-only session is not read-only")
            return Response({"detail": "The assistant's database session is not read-only, so it was not used."}, status=503)
        finally:
            release_lock(lock, token)

        # Buttons: files the theme tool returned in this request, and guide topics that were opened, each through
        # a whitelist and the member's permissions. A path never comes from the model's text.
        code = actions.code_file_actions(ctx.opened_files, ctx.read_theme_id, permissions)
        guide_buttons = actions.build_actions(ctx.open_keys, permissions)
        if code:  # the answer is about that file: a guide button (Pages, Customize...) would be a weak match
            guide_buttons = []
        body = {
            "answer": answer,
            "tools": used,
            "actions": (code + guide_buttons)[: actions.MAX_ACTIONS],
        }
        if ctx.proposals:
            body["proposal"] = ctx.proposals[-1]
        return Response(body)


class AssistantApplyView(StoreThemeView):
    """
    POST /api/v1/assistant/apply/   {path, start_line, end_line, old_text, new_text}
    Saves a change the assistant proposed, after the owner clicked Apply. Goes through the code editor's own save
    (history, live sync, undo), needs themes.manage, and refuses when the file changed since it was proposed.
    """

    action_permissions = {"POST": "themes.manage"}

    def initial(self, request, *args, **kwargs):
        if not getattr(settings, "ASSISTANT_REAL_MODE", False):
            raise Http404
        super().initial(request, *args, **kwargs)

    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        path = data.get("path")
        new_text, old_text = data.get("new_text"), data.get("old_text")
        start, end = data.get("start_line"), data.get("end_line")
        bad = exceptions.ValidationError({"detail": "That change is not valid."})
        if not themecode.valid_path(path) or path in themecode.NOT_CODE:
            raise bad
        if not isinstance(new_text, str) or not isinstance(old_text, str) or len(new_text) > themecode.MAX_EDIT_CHARS:
            raise bad
        if not isinstance(start, int) or not isinstance(end, int) or isinstance(start, bool) or isinstance(end, bool):
            raise bad
        if end - start + 1 > themecode.MAX_EDIT_LINES:
            raise bad

        store_theme = self.store_theme()
        current = store_theme.theme.files.filter(file_path=path).values_list("content", flat=True).first()
        edit = themecode.line_edit(current, start, end, new_text)
        if edit is None or edit[0] != old_text:
            raise exceptions.ValidationError({"detail": "The file changed since this was proposed. Ask again."})
        saved = save_file(store_theme, path, edit[1], client="assistant")
        return Response({"path": saved.file_path, "size": saved.file_size})
