"""
Checking and cleaning an uploaded picture. Every image upload (product photos, theme pictures, the media library)
goes through `clean_image`.

A picture is accepted only if Pillow can really decode it as a JPG, PNG or WEBP (not just if the first bytes look
right). It is then turned upright, and saved again without any EXIF / XMP / GPS / text data, in the same format.
"""
import io
import warnings

from PIL import Image, ImageOps
from rest_framework.exceptions import ValidationError

# Product photos: JPG, PNG or WEBP up to 1MB (the uploaded file, and the cleaned file that is kept)
MAX_IMAGE_SIZE = 1 * 1024 * 1024
# Width x height (x frames, for animated pictures). A small file can still unpack to gigabytes.
MAX_IMAGE_PIXELS = 25_000_000

IMAGE_SIGNATURES = {"jpg": (b"\xff\xd8\xff",), "png": (b"\x89PNG\r\n\x1a\n",)}

NOT_SUPPORTED = "Not a supported image. Use JPG, PNG or WEBP."
TOO_MANY_PIXELS = "Image is too large. Maximum is 25 million pixels (for example 5000 x 5000)."
TOO_BIG = "Image is too large. Maximum size is 1MB per image."

# What Pillow calls each format -> (our extension, content type)
FORMATS = {
    "JPEG": ("jpg", "image/jpeg"),
    "PNG": ("png", "image/png"),
    "WEBP": ("webp", "image/webp"),
}
_EXTENSION_TO_FORMAT = {ext: name for name, (ext, _) in FORMATS.items()}

# The only things kept from the original file's metadata: colours and animation timing, never personal data.
# Everything else (EXIF, XMP, GPS, comments, text chunks, camera data) is thrown away.
KEPT_INFO = ("icc_profile", "transparency", "duration", "loop")

# Saving again can make a lossy file bigger than the upload (the upload may have been compressed harder).
# If the cleaned file is over the limit, these lower qualities are tried before it is refused.
LOSSY_QUALITIES = (92, 85, 75, 65)


def public_image_url(request, url):
    """Absolute address to save for an uploaded picture: on PUBLIC_API_URL when set, else on the request's own host."""
    from django.conf import settings

    if url.startswith("/") and settings.PUBLIC_API_URL:
        return settings.PUBLIC_API_URL + url
    url = request.build_absolute_uri(url)
    # Behind a proxy Django sees plain http; an http image on the https storefront is blocked by the browser
    if not settings.DEBUG and url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    return url


def image_extension(head):
    """The kind of picture a file starts like ('jpg', 'png', 'webp') or None. Only a first, cheap check."""
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    for ext, signatures in IMAGE_SIGNATURES.items():
        if head.startswith(signatures):
            return ext
    return None


def _open(data):
    return Image.open(io.BytesIO(data), formats=list(FORMATS))


def _encode(image, name, options):
    """Save `image` in format `name`; returns the bytes, the first that fits MAX_IMAGE_SIZE (else the last tried)."""
    if name == "PNG":
        attempts = [options, {**options, "optimize": True}]  # lossless: only a slower, tighter save can shrink it
    elif name == "JPEG":
        attempts = [{**options, "quality": q, "optimize": True} for q in LOSSY_QUALITIES]
    else:
        attempts = [{**options, "quality": q} for q in LOSSY_QUALITIES]
    for attempt in attempts:
        out = io.BytesIO()
        image.save(out, format=name, **attempt)  # only what is passed here is written: no EXIF, XMP, GPS or text
        if out.tell() <= MAX_IMAGE_SIZE:
            break
    return out.getvalue()


def clean_image(data):
    """
    Validate and clean the bytes of an uploaded picture.
    Returns (clean bytes, extension, content type). Raises a ValidationError on {"image": ...} when the file is
    not a real JPG / PNG / WEBP, is damaged, has more than MAX_IMAGE_PIXELS pixels, or is still over
    MAX_IMAGE_SIZE after being saved again.
    """
    ext = image_extension(data[:12])
    if ext is None:
        raise ValidationError({"image": NOT_SUPPORTED})

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # odd metadata (e.g. a phone's EXIF) is dropped anyway, so don't fail on it
            probe = _open(data)
            frames = getattr(probe, "n_frames", 1)
            if probe.format != _EXTENSION_TO_FORMAT[ext]:
                raise ValueError("format does not match the file header")
            if probe.width * probe.height * frames > MAX_IMAGE_PIXELS:
                raise ValidationError({"image": TOO_MANY_PIXELS})
            probe.verify()  # checks the file's structure; the picture must be opened again afterwards

            image = _open(data)
            image.load()  # a real decode of the pixels: a damaged or cut-off file fails here
            animated = frames > 1
            durations = []
            if animated:
                # Every frame is checked; they are all written back below, each with its own display time
                durations.append(image.info.get("duration", 0))
                for index in range(1, frames):
                    image.seek(index)
                    image.load()
                    durations.append(image.info.get("duration", 0))
                image.seek(0)
            else:
                image = ImageOps.exif_transpose(image)  # phone photos: turn upright, drop the orientation flag

            # Forget all metadata (EXIF, XMP, GPS, comments ...) except what is kept on purpose
            image.info = {key: image.info[key] for key in KEPT_INFO if key in image.info}

            name = _EXTENSION_TO_FORMAT[ext]
            options = {}
            if "icc_profile" in image.info:
                options["icc_profile"] = image.info["icc_profile"]
            if name == "PNG" and "transparency" in image.info:
                options["transparency"] = image.info["transparency"]
            if animated:
                options["save_all"] = True
                options["duration"] = durations
                if "loop" in image.info:
                    options["loop"] = image.info["loop"]
                if name == "PNG":
                    options["default_image"] = False

            clean = _encode(image, name, options)
    except ValidationError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:  # Pillow's own, much higher limit
        raise ValidationError({"image": TOO_MANY_PIXELS}) from exc
    except Exception as exc:  # unreadable, damaged, cut off, or a bomb Pillow itself refuses
        raise ValidationError({"image": NOT_SUPPORTED}) from exc

    if len(clean) > MAX_IMAGE_SIZE:
        raise ValidationError({"image": TOO_BIG})
    return clean, ext, FORMATS[name][1]
