from django.urls import path

from .views import ChatView, DraftDiscardView, DraftPublishView

urlpatterns = [
    path("chat/", ChatView.as_view(), name="ai-assistant-chat"),
    path("drafts/<uuid:page_id>/publish/", DraftPublishView.as_view(), name="ai-assistant-publish"),
    path("drafts/<uuid:page_id>/discard/", DraftDiscardView.as_view(), name="ai-assistant-discard"),
]
