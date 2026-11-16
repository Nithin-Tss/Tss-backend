"""
FastAPI front end for the AI assistant, served from the same ASGI process as Django (see config/asgi.py).

Mounted at /fastapi/ai-assistant (docs: /fastapi/ai-assistant/docs):

    POST   /chat                           {message, conversation_id?} -> {reply, conversation_id, changes}
    GET    /conversations                  this person's chats in this store, newest first
    GET    /conversations/{id}             one chat with its messages
    DELETE /conversations/{id}             delete a chat (drafts it made stay, they just lose the link)
    GET    /drafts                         pages the assistant made or edited that aren't published yet
    POST   /drafts/{page_id}/publish       make a draft visible
    POST   /drafts/{page_id}/discard       delete (or restore) a draft

It is not a second copy of the rules. Authentication, store membership, role permissions, the feature flag, the
"no API keys" rule, throttling and audit logging all run the same classes the DRF API uses (views.py), so this
can never be more permissive than /api/v1/ai-assistant/. Send the same headers: `Authorization: Bearer <access
token>` and `X-Store-Id: <store uuid>`.

Endpoints are plain `def` (FastAPI runs them in its thread pool), because the Django ORM is synchronous.
"""
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Optional

from django.conf import settings
from django.db import close_old_connections
from django.http import HttpRequest
from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from rest_framework import exceptions
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request as DrfRequest
from rest_framework.settings import api_settings

from apps.audit import services as audit
from apps.core.authentication import JWTAuthentication
from apps.core.permissions import HasStorePermission, IsStoreMember
from apps.platform_admin.permissions import feature_required
from apps.themes.views import StorePageSerializer

from . import chat, llm, services
from .models import AiPageDraft, Message
from .views import FLAG, NoApiKeys, StoreThrottle

PREFIX = "/fastapi/ai-assistant"
MAX_LIST = 100


# ------------------------------------------------------------------ the bridge to the DRF rules

class _View:
    """What the DRF permission classes read off a view. Same as views.AiView."""

    permission_resource = "themes"
    action = None
    throttle_scope = None
    # Building pages needs the same permission as editing the theme, whatever the HTTP method
    action_permissions = {"POST": "themes.manage", "DELETE": "themes.manage"}


_PERMISSIONS = [IsAuthenticated, NoApiKeys, IsStoreMember, HasStorePermission, feature_required(FLAG)]


@dataclass
class Access:
    """Who is calling and for which store, once every check has passed."""

    store: Any
    user: Any
    request: DrfRequest  # a DRF request over a Django one: only for build_absolute_uri and the like


def _django_request(request: Request) -> HttpRequest:
    """A Django request carrying this request's method, path and headers (the DRF classes read those)."""
    http = HttpRequest()
    http.method = request.method
    http.path = http.path_info = request.url.path
    meta = {f"HTTP_{name.upper().replace('-', '_')}": value for name, value in request.headers.items()}
    meta["REMOTE_ADDR"] = request.client.host if request.client else ""
    meta["SERVER_NAME"] = request.url.hostname or "localhost"
    meta["SERVER_PORT"] = str(request.url.port or (443 if request.url.scheme == "https" else 80))
    meta["wsgi.url_scheme"] = request.url.scheme
    http.META = meta
    return http


def _check_permissions(request: DrfRequest, view: _View) -> None:
    for permission_class in _PERMISSIONS:
        permission = permission_class()
        if permission.has_permission(request, view):
            continue
        if not request.successful_authenticator:
            raise exceptions.NotAuthenticated()
        raise exceptions.PermissionDenied(detail=getattr(permission, "message", None))


def _check_throttles(request: DrfRequest, view: _View, throttle_classes) -> None:
    waits = []
    for throttle_class in throttle_classes:
        throttle = throttle_class()
        if not throttle.allow_request(request, view):
            waits.append(throttle.wait())
    if waits:
        raise exceptions.Throttled(wait=max((w for w in waits if w is not None), default=None))


@contextmanager
def session(request: Request, throttle_classes=None):
    """
    Everything the DRF view machinery does around a request, for one endpoint call: sign-in, store, permission,
    feature flag, throttle, then the audit entry once the outcome is known and the database connection cleanup.
    Use it inside the endpoint (not as a dependency) so all of it runs in the one worker thread that does the ORM work.
    """
    close_old_connections()
    http = _django_request(request)
    drf = DrfRequest(http, authenticators=[JWTAuthentication()])
    view = _View()
    status = 200
    try:
        _check_permissions(drf, view)
        _check_throttles(drf, view, api_settings.DEFAULT_THROTTLE_CLASSES if throttle_classes is None else throttle_classes)
        yield Access(store=drf.store, user=drf.user, request=drf)
    except exceptions.APIException as error:
        status = error.status_code
        raise
    except HTTPException as error:
        status = error.status_code
        raise
    except Exception:
        status = 500
        raise
    finally:
        try:
            audit.log_request(http, SimpleNamespace(status_code=status))
        finally:
            audit.end()
            close_old_connections()


# ------------------------------------------------------------------ response shapes

class ChatIn(BaseModel):
    message: Optional[str] = None
    conversation_id: Optional[str] = None


class ChatOut(BaseModel):
    reply: str
    conversation_id: str
    changes: list[dict[str, Any]]


class ConversationOut(BaseModel):
    id: str
    escalated: bool
    created_at: datetime
    updated_at: datetime
    message_count: int
    preview: str  # the first thing the merchant asked


class MessageOut(BaseModel):
    id: str
    role: str
    content: str
    changes: list[dict[str, Any]]
    created_at: datetime


class ConversationDetail(BaseModel):
    id: str
    created_at: datetime
    updated_at: datetime
    messages: list[MessageOut]


class DraftOut(BaseModel):
    page_id: str
    handle: str
    title: str
    status: str
    created_by_ai: bool
    conversation_id: Optional[str]
    created_at: datetime


# ------------------------------------------------------------------ the app

app = FastAPI(
    title="TSS AI assistant",
    description="Chat with the page-builder assistant and review the drafts it makes.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.CORS_ALLOWED_ORIGINS),
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=list(settings.CORS_ALLOW_HEADERS),
)


@app.exception_handler(exceptions.APIException)
def _api_exception(request: Request, error: exceptions.APIException):
    """DRF errors keep the shape the DRF API gives: {"detail": ...} or {field: [...]}, with the same headers."""
    headers = {}
    if isinstance(error, exceptions.Throttled) and error.wait is not None:
        headers["Retry-After"] = str(int(error.wait))
    if isinstance(error, exceptions.NotAuthenticated) or isinstance(error, exceptions.AuthenticationFailed):
        headers["WWW-Authenticate"] = JWTAuthentication().authenticate_header(None)
    return JSONResponse(error.detail, status_code=error.status_code, headers=headers)


def _uuid(value: str, what: str = "id") -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise exceptions.NotFound(f"That {what} doesn't exist.")


@app.post("/chat", response_model=ChatOut)
def post_chat(body: ChatIn, request: Request):
    with session(request, throttle_classes=[StoreThrottle]) as who:
        try:
            return chat.send_message(who.store, who.user, body.message, body.conversation_id)
        except llm.LLMError as error:
            raise HTTPException(status_code=503, detail=str(error))


@app.get("/conversations", response_model=list[ConversationOut])
def list_conversations(request: Request, limit: int = 30):
    with session(request) as who:
        rows = list(chat.own_conversations(who.store, who.user).order_by("-updated_at")[: max(1, min(limit, MAX_LIST))])
        out = []
        for conversation in rows:
            messages = conversation.messages.all()
            first = messages.filter(role=Message.USER).first()
            out.append(ConversationOut(
                id=str(conversation.pk), escalated=conversation.escalated,
                created_at=conversation.created_at, updated_at=conversation.updated_at,
                message_count=messages.count(), preview=(first.content[:120] if first else ""),
            ))
        return out


def _conversation(who: Access, conversation_id: str):
    conversation = chat.own_conversations(who.store, who.user).filter(pk=_uuid(conversation_id, "conversation")).first()
    if conversation is None:
        raise exceptions.NotFound("That conversation doesn't exist.")
    return conversation


@app.get("/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(conversation_id: str, request: Request):
    with session(request) as who:
        conversation = _conversation(who, conversation_id)
        return ConversationDetail(
            id=str(conversation.pk), created_at=conversation.created_at, updated_at=conversation.updated_at,
            messages=[
                MessageOut(id=str(m.pk), role=m.role, content=m.content, changes=m.changes, created_at=m.created_at)
                for m in conversation.messages.all()
            ],
        )


@app.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: str, request: Request):
    with session(request) as who:
        _conversation(who, conversation_id).delete()


@app.get("/drafts", response_model=list[DraftOut])
def list_drafts(request: Request):
    with session(request) as who:
        drafts = AiPageDraft.objects.filter(store=who.store).select_related("page").order_by("-created_at")[:MAX_LIST]
        return [
            DraftOut(
                page_id=str(d.page_id), handle=d.page.handle, title=d.page.title, status=d.page.status,
                created_by_ai=d.created_by_ai, conversation_id=str(d.conversation_id) if d.conversation_id else None,
                created_at=d.created_at,
            )
            for d in drafts
        ]


@app.post("/drafts/{page_id}/publish")
def publish_draft(page_id: str, request: Request) -> dict[str, Any]:
    with session(request) as who:
        page = services.publish_draft(who.store, _uuid(page_id, "draft"))
        return StorePageSerializer(page, context={"request": who.request}).data


@app.post("/drafts/{page_id}/discard")
def discard_draft(page_id: str, request: Request) -> dict[str, Any]:
    with session(request) as who:
        return services.discard_draft(who.store, _uuid(page_id, "draft"))
