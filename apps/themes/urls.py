from django.urls import path

from .views import ThemeFilesView, ThemeRenameView, ThemeRenderView

urlpatterns = [
    path("files/", ThemeFilesView.as_view()),
    path("files/rename/", ThemeRenameView.as_view()),
    path("render/", ThemeRenderView.as_view()),
]
