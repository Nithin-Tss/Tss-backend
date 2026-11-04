import json
import re
import uuid

from django.conf import settings
from django.db.models import Sum
from rest_framework.exceptions import ValidationError

from . import storage
from .images import MAX_IMAGE_SIZE, clean_image
from .models import MediaAsset

IMAGE_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


IMAGE_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def safe_name(name):
    """An uploaded file's name for display only: no folders, no control characters."""
    name = re.sub(r"[\x00-\x1f\x7f]", "", (name or "").replace("\\", "/").rsplit("/", 1)[-1]).strip()
    return (name or "image")[:255]


def check_quota(store_id, new_bytes):
    """Refuse an upload that would take the store over MEDIA_STORE_QUOTA_BYTES (media library and product photos)."""
    from apps.themes.models import ThemeImage

    quota = getattr(settings, "MEDIA_STORE_QUOTA_BYTES", 0)
    if not quota:
        return
    used = MediaAsset.objects.filter(store_id=store_id).aggregate(total=Sum("file_size"))["total"] or 0
    # Product photos are kept in ThemeImage, library pictures in MediaAsset
    used += ThemeImage.objects.filter(store_id=store_id).aggregate(total=Sum("size"))["total"] or 0
    if used + new_bytes > quota:
        raise ValidationError({"image": "Your media library is full. Delete unused pictures first."})


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
        original_name=safe_name(original_name),
        mime_type=content_type,
        file_size=size,
        uploaded_by=user if getattr(user, "is_authenticated", False) else None,
    )


def upload_image(store_id, file, user=None):
    """
    Validate and clean an uploaded picture (JPG, PNG or WEBP up to 1MB, really decoded and saved again without
    EXIF / GPS data, same rules as product photos), keep it in the database and list it as a media asset. Returns the MediaAsset.
    """
    if file is None:
        raise ValidationError({"image": "Choose an image to upload."})
    if file.size > MAX_IMAGE_SIZE:
        raise ValidationError({"image": "Image is too large. Maximum size is 1MB per image."})

    data, _, content_type = clean_image(file.read())

    check_quota(store_id, len(data))

    return store_image(store_id, data, content_type, file.name, user)


def asset_in_use(asset):
    """True if the store's themes (settings or code) point at this picture, so deleting it would leave a broken image."""
    from apps.themes.models import StoreTheme, ThemeFile

    key = str(asset.id)
    themes = StoreTheme.objects.filter(store_id=asset.store_id)
    if any(key in json.dumps(data or {}) for data in themes.values_list("custom_data", flat=True)):
        return True
    return ThemeFile.objects.filter(theme__in=[t.theme_id for t in themes], content__contains=key).exists()
