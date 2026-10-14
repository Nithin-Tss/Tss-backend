from django.urls import path
 
from .views import (
    BlogPostView,
    BlogPostsView,
    SectionHistoryView,
    SyncTimingsView,
    ThemeCustomizerView,
    ThemeEventsView,
    ThemeSectionsView,
    ThemeFilesView,
    ThemeImageView,
    ThemePreviewView,
    ThemeRenameView,
    ThemeSchemaView,
    ThemeSettingsView,
    StorefrontLinkView,
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

    path(
        "sections/",
        ThemeSectionsView.as_view(),
        name="theme_sections",
    ),

    path(
        "sections/<str:name>/history/",
        SectionHistoryView.as_view(),
        name="theme_section_history",
    ),

    path(
        "events/",
        ThemeEventsView.as_view(),
        name="theme_events",
    ),

    path(
        "sync-timings/",
        SyncTimingsView.as_view(),
        name="theme_sync_timings",
    ),

    path(
        "posts/",
        BlogPostsView.as_view(),
        name="theme_blog_posts",
    ),

    path(
        "posts/<uuid:post_id>/",
        BlogPostView.as_view(),
        name="theme_blog_post",
    ),

    path(
        "storefront-link/",
        StorefrontLinkView.as_view(),
        name="theme_storefront_link",
    ),
]
 
