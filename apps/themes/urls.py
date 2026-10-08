from django.urls import path

from .views import ThemeFileRenameView, ThemeFilesView, ThemeRenderView

urlpatterns = [
    path("files/", ThemeFilesView.as_view(), name="theme-files"),
    path("files/rename/", ThemeFileRenameView.as_view(), name="theme-file-rename"),
    path("render/", ThemeRenderView.as_view(), name="theme-render"),
]
