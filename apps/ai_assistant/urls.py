from django.urls import path

from .views import (
    ChatView,
    ConversationDetailView,
    ConversationListView,
    DraftDiscardView,
    DraftListView,
    DraftPublishView,
)

urlpatterns = [
    path("chat/", ChatView.as_view(), name="ai-assistant-chat"),
    path("conversations/", ConversationListView.as_view(), name="ai-assistant-conversations"),
    path("conversations/<str:conversation_id>/", ConversationDetailView.as_view(), name="ai-assistant-conversation"),
    path("drafts/", DraftListView.as_view(), name="ai-assistant-drafts"),
    path("drafts/<uuid:page_id>/publish/", DraftPublishView.as_view(), name="ai-assistant-publish"),
    path("drafts/<uuid:page_id>/discard/", DraftDiscardView.as_view(), name="ai-assistant-discard"),
]
