"""
POST /api/v1/ai-assistant/chat/                       {message, conversation_id?} -> {reply, conversation_id, changes}
POST /api/v1/ai-assistant/drafts/<page_id>/publish/   make an assistant draft visible
POST /api/v1/ai-assistant/drafts/<page_id>/discard/   delete (or restore) an assistant draft

Needs the themes.manage permission and the ai_page_builder feature flag (off by default: 403 while off).
"""
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from apps.core.permissions import HasStorePermission, IsStoreMember
from apps.platform_admin.models import ApiKey
from apps.platform_admin.permissions import feature_required
from apps.themes.views import StorePageSerializer

from . import chat, llm, services

FLAG = "ai_page_builder"


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


class ChatView(AiView):
    throttle_classes = [StoreThrottle]

    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        try:
            result = chat.send_message(request.store, request.user, data.get("message"), data.get("conversation_id"))
        except llm.LLMError as error:
            return Response({"detail": str(error)}, status=503)
        return Response(result)


class DraftPublishView(AiView):
    def post(self, request, page_id):
        page = services.publish_draft(request.store, page_id)
        return Response(StorePageSerializer(page, context={"request": request}).data)


class DraftDiscardView(AiView):
    def post(self, request, page_id):
        return Response(services.discard_draft(request.store, page_id))
