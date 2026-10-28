import json

from rest_framework.exceptions import ValidationError

from .models import MediaAsset

IMAGE_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


def store_image(store_id, data, content_type, original_name, user=None, asset_id=None):
    """Keep a picture's bytes in the store's media assets (one row, in the database)."""
    extra = {"id": asset_id} if asset_id else {}
    return MediaAsset.objects.create(
        store_id=store_id,
        data=data,
        original_name=(original_name or "image")[:255],
        mime_type=content_type,
        file_size=len(data),
        uploaded_by=user if getattr(user, "is_authenticated", False) else None,
        **extra,
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

    return store_image(store_id, file.read(), IMAGE_TYPES[ext], file.name, user)


def asset_in_use(asset):
    """True if the store's themes (settings or code) point at this picture, so deleting it would leave a broken image."""
    from apps.themes.models import StoreTheme, ThemeFile

    key = str(asset.id)
    themes = StoreTheme.objects.filter(store_id=asset.store_id)
    if any(key in json.dumps(data or {}) for data in themes.values_list("custom_data", flat=True)):
        return True
    return ThemeFile.objects.filter(theme__in=[t.theme_id for t in themes], content__contains=key).exists()
