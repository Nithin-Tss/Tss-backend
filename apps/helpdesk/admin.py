from django.contrib import admin

from .models import HelpArticle


@admin.register(HelpArticle)
class HelpArticleAdmin(admin.ModelAdmin):
    list_display = ("title", "category", "is_published", "updated_at")
    list_filter = ("category", "is_published")
    search_fields = ("title", "keywords", "body")
    prepopulated_fields = {"slug": ("title",)}
    actions = ["publish", "unpublish"]

    @admin.action(description="Publish selected articles")
    def publish(self, request, queryset):
        queryset.update(is_published=True)

    @admin.action(description="Unpublish selected articles")
    def unpublish(self, request, queryset):
        queryset.update(is_published=False)
