import json
import uuid

from django.conf import settings
from django.db.models import Sum
from rest_framework.exceptions import ValidationError

from . import storage
from .models import MediaAsset

IMAGE_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


IMAGE_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def store_image(store_id, data, content_type, original_name, user=None, asset_id=None):
    """Keep a picture in the store's media assets: in the bucket if one is configured, else in the database."""
    asset_id = asset_id or uuid.uuid4()
    size = len(data)
    storage_key = None
    if storage.is_configured():
        storage_key = f"uploads/{store_id}/{asset_id.hex}{IMAGE_EXTENSIONS.get(content_type, '')}"
        try:
            storage.put(storage_key, data, content_type)
        except Exception as exc:
            raise ValidationError({"image": "Could not save the image. Please try again."}) from exc
        data = None
    return MediaAsset.objects.create(
        id=asset_id,
        store_id=store_id,
        data=data,
        storage_key=storage_key,
        original_name=(original_name or "image")[:255],
        mime_type=content_type,
        file_size=size,
        uploaded_by=user if getattr(user, "is_authenticated", False) else None,
    )


def upload_image(store_id, file, user=None):
    """
    Validate an uploaded picture (JPG, PNG or WEBP up to 1MB, checked by its first bytes, same rules as product
    photos), keep it in the database and list it as a media asset. Returns the MediaAsset.
    """
    from apps.catalog.views import MAX_IMAGE_SIZE, image_extension

    if file is None:
        raise ValidationError({"image": "Choose an image to upload."})
    if file.size > MAX_IMAGE_SIZE:
        raise ValidationError({"image": "Image is too large. Maximum size is 1MB per image."})

    ext = image_extension(file.read(12))
    if ext is None:
        raise ValidationError({"image": "Not a supported image. Use JPG, PNG or WEBP."})
    file.seek(0)

    quota = getattr(settings, "MEDIA_STORE_QUOTA_BYTES", 0)
    if quota:
        used = MediaAsset.objects.filter(store_id=store_id).aggregate(total=Sum("file_size"))["total"] or 0
        if used + file.size > quota:
            raise ValidationError({"image": "Your media library is full. Delete unused pictures first."})

    return store_image(store_id, file.read(), IMAGE_TYPES[ext], file.name, user)


def asset_in_use(asset):
    """True if the store's themes (settings or code) point at this picture, so deleting it would leave a broken image."""
    from apps.themes.models import StoreTheme, ThemeFile

    key = str(asset.id)
    themes = StoreTheme.objects.filter(store_id=asset.store_id)
    if any(key in json.dumps(data or {}) for data in themes.values_list("custom_data", flat=True)):
        return True
    return ThemeFile.objects.filter(theme__in=[t.theme_id for t in themes], content__contains=key).exists()
