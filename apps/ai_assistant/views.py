"""
POST /api/v1/ai-assistant/chat/                       {message, conversation_id?} -> {reply, conversation_id, changes}
POST /api/v1/ai-assistant/drafts/<page_id>/publish/   make an assistant draft visible
POST /api/v1/ai-assistant/drafts/<page_id>/discard/   delete (or restore) an assistant draft

Needs the themes.manage permission and the ai_page_builder feature flag (off by default: 403 while off).
"""
import uuid

from django.http import Http404
from rest_framework import exceptions
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from apps.core.permissions import HasStorePermission, IsStoreMember
from apps.platform_admin.models import ApiKey
from apps.platform_admin.permissions import feature_required
from apps.themes.views import StorePageSerializer

from . import agent, llm, services, tools
from .models import Conversation, Message

FLAG = "ai_page_builder"
MAX_MESSAGE_CHARS = 2000
HISTORY_MESSAGES = 12


class NoApiKeys(BasePermission):
    message = "The AI assistant can't be used with an API key."

    def has_permission(self, request, view):
        return not isinstance(request.auth, ApiKey)


class StoreThrottle(SimpleRateThrottle):
    """Per merchant (store), not per person: every chat message costs a paid model call."""

    scope = "ai_assistant"

    def get_cache_key(self, request, view):
        store = getattr(request, "store", None)
        return self.cache_format % {"scope": self.scope, "ident": store.pk if store else self.get_ident(request)}


class AiView(APIView):
    permission_classes = [IsAuthenticated, NoApiKeys, IsStoreMember, HasStorePermission, feature_required(FLAG)]
    permission_resource = "themes"
    # Building pages needs the same permission as editing the theme, whatever the HTTP method
    action_permissions = {"POST": "themes.manage"}


def _history(conversation):
    """The last turns as plain chat messages. Changes are noted in text so follow-ups can refer to the page ids."""
    rows = list(conversation.messages.order_by("-created_at")[:HISTORY_MESSAGES])[::-1]
    out = []
    for row in rows:
        content = row.content
        if row.changes:
            notes = "; ".join(f"{c['summary']} (page_id {c['page_id']})" for c in row.changes)
            content = f"{content}\n[Changes made: {notes}]"
        out.append({"role": row.role, "content": content})
    return out


class ChatView(AiView):
    throttle_classes = [StoreThrottle]

    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        message = data.get("message")
        if not isinstance(message, str) or not message.strip():
            raise exceptions.ValidationError({"message": "Type a message."})
        message = message.strip()
        if len(message) > MAX_MESSAGE_CHARS:
            raise exceptions.ValidationError({"message": f"Keep it under {MAX_MESSAGE_CHARS} characters."})

        raw_id = data.get("conversation_id")
        if raw_id:
            try:
                wanted = uuid.UUID(str(raw_id))
            except ValueError:
                raise exceptions.ValidationError({"conversation_id": "Not a valid conversation id."})
            # Both store and user: another merchant's (or colleague's) conversation looks like it doesn't exist
            conversation = Conversation.objects.filter(
                pk=wanted, store=request.store, user=request.user, assistant_type=Conversation.PAGE_BUILDER,
            ).first()
            if conversation is None:
                raise exceptions.NotFound("That conversation doesn't exist.")
        else:
            conversation = Conversation.objects.create(store=request.store, user=request.user)

        history = _history(conversation)
        ctx = tools.Context(store=request.store, user=request.user, conversation=conversation)
        try:
            reply = agent.run(ctx, history, message)
        except llm.LLMError as error:
            return Response({"detail": str(error)}, status=503)

        Message.objects.create(conversation=conversation, role=Message.USER, content=message)
        Message.objects.create(conversation=conversation, role=Message.ASSISTANT, content=reply, changes=ctx.changes)
        conversation.save(update_fields=["updated_at"])
        return Response({"reply": reply, "conversation_id": str(conversation.pk), "changes": ctx.changes})


class DraftPublishView(AiView):
    def post(self, request, page_id):
        page = services.publish_draft(request.store, page_id)
        return Response(StorePageSerializer(page, context={"request": request}).data)


class DraftDiscardView(AiView):
    def post(self, request, page_id):
        return Response(services.discard_draft(request.store, page_id))
