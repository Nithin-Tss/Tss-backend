from django.core.cache import cache
from django.db.models import Q
from rest_framework import mixins, status
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework import serializers
from rest_framework.response import Response

from apps.core.viewsets import StoreScopedGenericViewSet

from .models import MediaAsset
from .services import asset_in_use, upload_image


class MediaAssetSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source="original_name", max_length=255)
    url = serializers.SerializerMethodField()
    uploaded_by_name = serializers.SerializerMethodField()

    class Meta:
        model = MediaAsset
        fields = ["id", "name", "url", "mime_type", "file_size", "alt_text", "uploaded_by_name", "created_at"]
        read_only_fields = ["id", "url", "mime_type", "file_size", "uploaded_by_name", "created_at"]

    def get_url(self, obj):
        request = self.context.get("request")
        return request.build_absolute_uri(obj.url) if request and obj.url else obj.url

    def get_uploaded_by_name(self, obj):
        user = obj.uploaded_by
        return (f"{user.first_name} {user.last_name}".strip() or user.email) if user else None


class MediaAssetViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    StoreScopedGenericViewSet,
):
    """
    /api/v1/media/       GET list (?q= searches name and alt text), POST upload (multipart, field "image")
    /api/v1/media/<id>/  GET, PATCH name and alt_text, DELETE
    The pictures a store uploads outside the product form (banners, slides, image blocks). Part of the online store area.
    """

    permission_resource = "themes"
    queryset = MediaAsset.objects.select_related("uploaded_by").defer("data")
    serializer_class = MediaAssetSerializer
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        qs = super().get_queryset()
        term = (self.request.query_params.get("q") or "").strip()
        if term:
            qs = qs.filter(Q(original_name__icontains=term) | Q(alt_text__icontains=term))
        return qs

    def create(self, request, *args, **kwargs):
        asset = upload_image(request.store.store_id, request.FILES.get("image"), request.user)
        return Response(self.get_serializer(asset).data, status=status.HTTP_201_CREATED)

    def perform_destroy(self, instance):
        if asset_in_use(instance):
            raise ValidationError("This picture is used in your online store. Remove it there first.")
        key = f"theme-image:{instance.id}"
        instance.delete()
        cache.delete(key)  # a deleted picture is no longer served
