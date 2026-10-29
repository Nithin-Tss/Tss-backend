from django.contrib import admin

from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """Read-only: the log is evidence, so nobody edits or deletes it from here."""

    list_display = ("created_at", "action", "actor_email", "store", "object_repr", "status_code")
    list_filter = ("action", "resource")
    search_fields = ("actor_email", "action", "object_repr", "object_id")
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
