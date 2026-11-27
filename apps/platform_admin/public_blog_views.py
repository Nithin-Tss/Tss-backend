from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import PlatformBlogPost
from .serializers import PlatformBlogPostPublicSerializer


class PublicBlogPostListView(APIView):
    """
    GET /api/v1/blog/ or /api/v1/blog/posts/
    Public list of published platform blog posts.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        now = timezone.now()
        qs = PlatformBlogPost.objects.filter(
            status=PlatformBlogPost.PUBLISHED,
            published_at__lte=now,
        ).order_by("-published_at")

        if category := request.query_params.get("category"):
            qs = qs.filter(category__iexact=category)

        if q := request.query_params.get("q"):
            qs = qs.filter(
                Q(title__icontains=q) | Q(excerpt__icontains=q) | Q(content__icontains=q)
            )

        limit_raw = request.query_params.get("limit")
        offset_raw = request.query_params.get("offset")

        total = qs.count()
        if limit_raw is not None or offset_raw is not None:
            try:
                limit = max(1, min(100, int(limit_raw or 20)))
            except ValueError:
                limit = 20
            try:
                offset = max(0, int(offset_raw or 0))
            except ValueError:
                offset = 0
            page_qs = qs[offset:offset + limit]
        else:
            limit = total
            offset = 0
            page_qs = qs

        serializer = PlatformBlogPostPublicSerializer(page_qs, many=True)
        return Response({
            "count": total,
            "limit": limit,
            "offset": offset,
            "posts": serializer.data,
            "results": serializer.data,
        })


class PublicBlogPostDetailView(APIView):
    """
    GET /api/v1/blog/<slug>/
    Public single article detail by slug. Returns 404 if draft, future-dated, or nonexistent.
    """
    permission_classes = [AllowAny]

    def get(self, request, slug):
        now = timezone.now()
        post = PlatformBlogPost.objects.filter(
            slug=slug,
            status=PlatformBlogPost.PUBLISHED,
            published_at__lte=now,
        ).first()

        if not post:
            raise NotFound("Blog post not found.")

        return Response(PlatformBlogPostPublicSerializer(post).data)


class PublicBlogCategoryListView(APIView):
    """
    GET /api/v1/blog/categories/
    List of distinct categories across all published blog articles.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        now = timezone.now()
        categories = (
            PlatformBlogPost.objects.filter(
                status=PlatformBlogPost.PUBLISHED,
                published_at__lte=now,
            )
            .exclude(category="")
            .values_list("category", flat=True)
            .distinct()
            .order_by("category")
        )
        return Response({"categories": list(categories)})
