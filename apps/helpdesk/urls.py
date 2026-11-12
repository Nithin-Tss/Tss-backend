"""Mounted at /api/v1/ (see config/api_v1.py): /support/tickets/... for owners, /platform/support/... for staff,
/help-assistant/... for the merchant help chat (help_views.py)."""
from django.urls import path

from . import help_views as h
from . import staff_views as s
from . import views as v

urlpatterns = [
    path("help-assistant/chat/", h.ChatView.as_view(), name="help_assistant_chat"),
    path("help-assistant/conversations/", h.ConversationListView.as_view(), name="help_assistant_conversations"),
    path("help-assistant/conversations/<str:conversation_id>/escalate/", h.EscalateView.as_view(), name="help_assistant_escalate"),
    path("help-assistant/conversations/<uuid:conversation_id>/", h.ConversationView.as_view(), name="help_assistant_conversation"),
    path("help-assistant/articles/<slug:slug>/", h.ArticleView.as_view(), name="help_assistant_article"),
    path("support/tickets/", v.TicketListCreateView.as_view(), name="support_tickets"),
    path("support/tickets/<uuid:ticket_id>/", v.TicketDetailView.as_view(), name="support_ticket"),
    path("support/tickets/<uuid:ticket_id>/messages/", v.TicketReplyView.as_view(), name="support_ticket_reply"),
    path("support/attachments/", v.AttachmentView.as_view(), name="support_attachment"),
    path("platform/support/summary/", s.StaffSummaryView.as_view(), name="platform_support_summary"),
    path("platform/support/agents/", s.StaffAgentsView.as_view(), name="platform_support_agents"),
    path("platform/support/tickets/", s.StaffTicketListView.as_view(), name="platform_support_tickets"),
    path(
        "platform/support/tickets/<uuid:ticket_id>/", s.StaffTicketDetailView.as_view(),
        name="platform_support_ticket",
    ),
    path(
        "platform/support/tickets/<uuid:ticket_id>/messages/", s.StaffTicketReplyView.as_view(),
        name="platform_support_reply",
    ),
    path(
        "platform/support/tickets/<uuid:ticket_id>/attachments/", s.StaffTicketAttachmentView.as_view(),
        name="platform_support_attachment",
    ),
]
