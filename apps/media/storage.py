"""
Optional external storage for media assets (any S3-compatible bucket: AWS S3, Cloudflare R2, DigitalOcean Spaces, MinIO).

Set MEDIA_S3_BUCKET (and the other MEDIA_S3_* settings) to turn it on. Without it, pictures stay in the database.
"""
from django.conf import settings


def is_configured():
    return bool(getattr(settings, "MEDIA_S3_BUCKET", ""))


def _client():
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=settings.MEDIA_S3_ENDPOINT_URL or None,
        region_name=settings.MEDIA_S3_REGION or None,
        aws_access_key_id=settings.MEDIA_S3_ACCESS_KEY_ID or None,
        aws_secret_access_key=settings.MEDIA_S3_SECRET_ACCESS_KEY or None,
    )


def public_url(key):
    """Address the browser loads the object from (the CDN / public bucket URL)."""
    base = settings.MEDIA_S3_PUBLIC_URL.rstrip("/")
    if not base:
        endpoint = (settings.MEDIA_S3_ENDPOINT_URL or "https://s3.amazonaws.com").rstrip("/")
        base = f"{endpoint}/{settings.MEDIA_S3_BUCKET}"
    return f"{base}/{key}"


def put(key, data, content_type):
    _client().put_object(
        Bucket=settings.MEDIA_S3_BUCKET,
        Key=key,
        Body=data,
        ContentType=content_type,
        CacheControl="public, max-age=31536000, immutable",
    )


def get(key):
    return _client().get_object(Bucket=settings.MEDIA_S3_BUCKET, Key=key)["Body"].read()


def delete(key):
    _client().delete_object(Bucket=settings.MEDIA_S3_BUCKET, Key=key)
