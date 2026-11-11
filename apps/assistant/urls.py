from django.urls import path

from .views import AssistantApplyView, AssistantChatView

urlpatterns = [
    path("chat/", AssistantChatView.as_view(), name="assistant-chat"),
    path("apply/", AssistantApplyView.as_view(), name="assistant-apply"),
]
