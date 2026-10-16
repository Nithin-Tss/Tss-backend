from django.urls import path
 
from .views import (
    BlogPostView,
    BlogPostsView,
    MenuView,
    MenusView,
    SectionHistoryView,
    SyncTimingsView,
    ThemeCustomizerView,
    ThemeEventsView,
    ThemeSectionsView,
    ThemeFilesView,
    ThemeImageView,
    ThemeLibraryItemView,
    ThemeLibraryView,
    ThemePublishView,
    ThemePreviewView,
    ThemeRenameView,
    PageTemplatesView,
    StorePagesView,
    StorePageView,
    ThemeSchemaView,
    ThemeSettingsView,
    StorefrontLinkView,
    UrlRedirectView,
    UrlRedirectsView,
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
        "library/",
        ThemeLibraryView.as_view(),
        name="theme_library",
    ),

    path(
        "library/<uuid:theme_id>/",
        ThemeLibraryItemView.as_view(),
        name="theme_library_item",
    ),

    path(
        "library/<uuid:theme_id>/publish/",
        ThemePublishView.as_view(),
        name="theme_library_publish",
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
        "pages/",
        StorePagesView.as_view(),
        name="theme_pages",
    ),

    path(
        "pages/<uuid:page_id>/",
        StorePageView.as_view(),
        name="theme_page",
    ),

    path(
        "menus/",
        MenusView.as_view(),
        name="theme_menus",
    ),

    path(
        "menus/<uuid:menu_id>/",
        MenuView.as_view(),
        name="theme_menu",
    ),

    path(
        "redirects/",
        UrlRedirectsView.as_view(),
        name="theme_redirects",
    ),

    path(
        "redirects/<uuid:redirect_id>/",
        UrlRedirectView.as_view(),
        name="theme_redirect",
    ),

    path(
        "page-templates/",
        PageTemplatesView.as_view(),
        name="theme_page_templates",
    ),

    path(
        "storefront-link/",
        StorefrontLinkView.as_view(),
        name="theme_storefront_link",
    ),
]
 
