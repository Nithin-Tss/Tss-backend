from django.contrib import admin

from .models import MediaAsset


@admin.register(MediaAsset)
class MediaAssetAdmin(admin.ModelAdmin):
    list_display = ("original_name", "store", "mime_type", "file_size", "uploaded_by", "created_at")
    list_filter = ("mime_type",)
    exclude = ("data",)
    search_fields = ("original_name", "alt_text")
    raw_id_fields = ("store", "uploaded_by")
