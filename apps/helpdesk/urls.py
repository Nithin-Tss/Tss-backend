"""Mounted at /api/v1/ (see config/api_v1.py): /support/tickets/... for owners, /platform/support/... for staff."""
from django.urls import path

from . import staff_views as s
from . import views as v

urlpatterns = [
    path("support/tickets/", v.TicketListCreateView.as_view(), name="support_tickets"),
    path("support/tickets/<uuid:ticket_id>/", v.TicketDetailView.as_view(), name="support_ticket"),
    path("support/tickets/<uuid:ticket_id>/messages/", v.TicketReplyView.as_view(), name="support_ticket_reply"),
    path("support/attachments/", v.AttachmentView.as_view(), name="support_attachment"),
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
