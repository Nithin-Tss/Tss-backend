from django.urls import path
from . import public_blog_views as views

urlpatterns = [
    path("", views.PublicBlogPostListView.as_view(), name="public_blog_list"),
    path("posts/", views.PublicBlogPostListView.as_view(), name="public_blog_posts"),
    path("categories/", views.PublicBlogCategoryListView.as_view(), name="public_blog_categories"),
    path("<slug:slug>/", views.PublicBlogPostDetailView.as_view(), name="public_blog_detail"),
]
