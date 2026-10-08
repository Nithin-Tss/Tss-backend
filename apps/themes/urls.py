from django.urls import path

from .views import ThemeFilesView, ThemeRenameView, ThemeSchemaView, ThemeSettingsView


urlpatterns = [
    path("files/", ThemeFilesView.as_view(), name="theme-files"),
    path("files/rename/", ThemeRenameView.as_view(), name="theme-files-rename"),
    path("settings/", ThemeSettingsView.as_view(), name="theme-settings"),
    path("schema/", ThemeSchemaView.as_view(), name="theme-schema"),
]
