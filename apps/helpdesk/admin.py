from django.contrib import admin

from . import indexing
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
        self._reindex(queryset)

    @admin.action(description="Unpublish selected articles")
    def unpublish(self, request, queryset):
        queryset.update(is_published=False)
        self._reindex(queryset)

    @staticmethod
    def _reindex(queryset):
        # queryset.update() sends no save signal, so the passages are rebuilt here
        if indexing.enabled():
            for article in queryset.model.objects.filter(pk__in=list(queryset.values_list("pk", flat=True))):
                indexing.index_article(article)
