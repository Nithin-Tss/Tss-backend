from django import forms
from django.contrib import admin

from apps.core.admin_guards import SameStoreForm, StoreGuardAdmin
from .models import BlogPost, Menu, MenuItem, StorePage, StoreTheme, Theme, ThemeFile, UrlRedirect


# Theme and ThemeFile are the platform's shared theme library, not owned by a store
admin.site.register(Theme)
admin.site.register(ThemeFile)


@admin.register(StoreTheme)
class StoreThemeAdmin(StoreGuardAdmin):
    list_display = ("__str__", "store_name", "theme", "is_active", "created_by")
    readonly_fields = ("created_by",)


@admin.register(BlogPost)
class BlogPostAdmin(StoreGuardAdmin):
    list_display = ("title", "store_name", "status", "published_at")
    list_filter = ("status",)
    search_fields = ("title", "handle", "store__store_name")


@admin.register(StorePage)
class StorePageAdmin(StoreGuardAdmin):
    list_display = ("title", "store_name", "status", "published_at")
    list_filter = ("status",)
    search_fields = ("title", "handle", "store__store_name")


@admin.register(Menu)
class MenuAdmin(StoreGuardAdmin):
    list_display = ("name", "store_name", "handle")
    search_fields = ("name", "handle", "store__store_name")


class MenuItemForm(SameStoreForm):
    def clean(self):
        cleaned = super().clean()
        menu = cleaned.get("menu")
        parent = cleaned.get("parent")
        if menu is not None and parent is not None and parent.menu_id != menu.pk:
            self.add_error("parent", "The parent item must belong to the same menu.")
        return cleaned


@admin.register(MenuItem)
class MenuItemAdmin(StoreGuardAdmin):
    form = MenuItemForm
    store_path = "menu__store"
    parent_fields = ("menu",)
    store_links = {"menu": "store", "parent": "menu.store"}


@admin.register(UrlRedirect)
class UrlRedirectAdmin(StoreGuardAdmin):
    list_display = ("path", "target", "store_name", "updated_at")
    search_fields = ("path", "target", "store__store_name")
