from django.urls import path

from .views import AuditLogDetailView, AuditLogExportView, AuditLogListView, MyActivityView

urlpatterns = [
    path("logs/", AuditLogListView.as_view(), name="audit_log_list"),
    path("logs/export/", AuditLogExportView.as_view(), name="audit_log_export"),
    path("logs/<uuid:entry_id>/", AuditLogDetailView.as_view(), name="audit_log_detail"),
    path("my-activity/", MyActivityView.as_view(), name="audit_my_activity"),
]
