import re

from django.utils import timezone
from rest_framework import serializers

from apps.tenancy.access import membership_permissions

from . import scopes
from .models import ApiKey, FeatureFlag, PlatformBlogPost
from apps.themes.schemas import sanitize_html

MAX_ACTIVE_KEYS = 25


class ApiKeySerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(read_only=True)
    storeId = serializers.UUIDField(source="store_id", read_only=True)
    storeName = serializers.CharField(source="store.store_name", read_only=True)
    scopes = serializers.ListField(child=serializers.CharField(), allow_empty=False)
    expiresAt = serializers.DateTimeField(source="expires_at", required=False, allow_null=True)
    status = serializers.CharField(read_only=True)
    lastUsedAt = serializers.DateTimeField(source="last_used_at", read_only=True)
    lastUsedIp = serializers.CharField(source="last_used_ip", read_only=True)
    revokedAt = serializers.DateTimeField(source="revoked_at", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    createdBy = serializers.SerializerMethodField()

    class Meta:
        model = ApiKey
        fields = [
            "id", "storeId", "storeName", "name", "prefix", "scopes", "expiresAt", "status",
            "lastUsedAt", "lastUsedIp", "revokedAt", "createdAt", "createdBy",
        ]
        read_only_fields = ["prefix"]

    def get_createdBy(self, key):
        return key.created_by.email if key.created_by else None

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Give the key a name.")
        return value

    def validate_scopes(self, value):
        unknown = sorted(set(value) - scopes.SCOPES)
        if unknown:
            raise serializers.ValidationError(f"Not a scope an API key can have: {', '.join(unknown)}.")
        # Nobody can hand out access they don't hold themselves (owners hold everything)
        membership = getattr(self.context.get("request"), "membership", None)
        if membership is not None:
            missing = sorted(set(scopes.grantable(value)) - membership_permissions(membership))
            if missing:
                raise serializers.ValidationError(f"You can't give a key access you don't have: {', '.join(missing)}.")
        return scopes.grantable(value)

    def validate_expiresAt(self, value):
        if value is not None and value <= timezone.now():
            raise serializers.ValidationError("The expiry must be in the future.")
        return value


FLAG_KEY = re.compile(r"^[a-z][a-z0-9_]{1,79}$")


class FeatureFlagSerializer(serializers.ModelSerializer):
    rolloutPercent = serializers.IntegerField(source="rollout_percent", min_value=0, max_value=100, required=False)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)
    overrideCount = serializers.SerializerMethodField()

    class Meta:
        model = FeatureFlag
        fields = ["key", "name", "description", "enabled", "rolloutPercent", "overrideCount", "createdAt", "updatedAt"]
        # The key is what code asks about, so it is fixed once made
        extra_kwargs = {"key": {"validators": []}}

    def get_overrideCount(self, flag):
        return flag.overrides.count()

    def validate_key(self, value):
        if self.instance is not None and value != self.instance.key:
            raise serializers.ValidationError("A flag's key can't be changed.")
        if not FLAG_KEY.match(value):
            raise serializers.ValidationError("Use lower-case letters, digits and underscores, starting with a letter.")
        if self.instance is None and FeatureFlag.objects.filter(key=value).exists():
            raise serializers.ValidationError("A flag with this key already exists.")
        return value


class PlatformBlogPostAdminSerializer(serializers.ModelSerializer):
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = PlatformBlogPost
        fields = [
            "id", "title", "slug", "category", "excerpt", "content",
            "featured_image", "author", "read_time", "status",
            "published_at", "created_by", "created_by_name",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_by", "created_at", "updated_at"]

    def get_created_by_name(self, obj):
        if not obj.created_by:
            return None
        name = f"{obj.created_by.first_name} {obj.created_by.last_name}".strip()
        return name or obj.created_by.email

    def validate_title(self, value):
        val = (value or "").strip()
        if not val:
            raise serializers.ValidationError("Title is required.")
        return val

    def validate_slug(self, value):
        from django.utils.text import slugify
        slug = slugify(value or "").strip("-")[:255]
        if not slug:
            raise serializers.ValidationError("Valid slug is required.")
        qs = PlatformBlogPost.objects.filter(slug=slug)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("Another article already uses this URL slug.")
        return slug

    def validate(self, attrs):
        from django.utils.text import slugify
        import re

        # Auto-generate slug from title if not provided
        if "slug" not in attrs and not (self.instance and self.instance.slug):
            title = attrs.get("title") or (self.instance.title if self.instance else "")
            base_slug = slugify(title)[:200].strip("-") or "article"
            slug = base_slug
            n = 2
            qs = PlatformBlogPost.objects.all()
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            while qs.filter(slug=slug).exists():
                slug = f"{base_slug}-{n}"
                n += 1
            attrs["slug"] = slug

        # Sanitize HTML content to prevent XSS
        if "content" in attrs:
            attrs["content"] = sanitize_html(attrs["content"] or "")

        # Auto-compute read_time if missing or empty
        content = attrs.get("content") or (self.instance.content if self.instance else "")
        if not attrs.get("read_time") and content:
            words = len(re.sub(r"<[^>]+>", " ", content).split())
            attrs["read_time"] = f"{max(1, round(words / 200))} min read"

        # Manage publication date
        status = attrs.get("status", self.instance.status if self.instance else PlatformBlogPost.DRAFT)
        if status == PlatformBlogPost.PUBLISHED:
            if not (self.instance and self.instance.published_at) and not attrs.get("published_at"):
                attrs["published_at"] = timezone.now()

        return attrs


class PlatformBlogPostPublicSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlatformBlogPost
        fields = [
            "id", "title", "slug", "category", "excerpt", "content",
            "featured_image", "author", "read_time", "published_at",
        ]
        read_only_fields = fields

