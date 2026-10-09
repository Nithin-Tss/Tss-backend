from django.urls import path
 
from .views import (
    ThemeCustomizerView,
    ThemeFilesView,
    ThemeImageView,
    ThemePreviewView,
    ThemeRenameView,
    ThemeSchemaView,
    ThemeSettingsView,
    theme_css,
)
 
urlpatterns = [
    path(
        "theme-css/<uuid:theme_id>/",
        theme_css,
        name="theme_css",
    ),
 
    path(
        "files/",
        ThemeFilesView.as_view(),
        name="theme_files",
    ),
 
    path(
        "files/rename/",
        ThemeRenameView.as_view(),
        name="theme_rename",
    ),
 
    path(
        "settings/",
        ThemeSettingsView.as_view(),
        name="theme_settings",
    ),
 
    path(
        "customizer/",
        ThemeCustomizerView.as_view(),
        name="theme_customizer",
    ),
 
    path(
        "schema/",
        ThemeSchemaView.as_view(),
        name="theme_schema",
    ),

    path(
        "images/",
        ThemeImageView.as_view(),
        name="theme_images",
    ),

    path(
        "preview/",
        ThemePreviewView.as_view(),
        name="theme_preview",
    ),
]
 
