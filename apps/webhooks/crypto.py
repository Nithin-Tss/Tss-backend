"""
Authenticated encryption for webhook endpoint secrets, built on the standard library only
(HMAC-SHA256 in counter mode for the keystream, encrypt-then-MAC with a second derived key).

Keys come from the environment: WEBHOOK_SECRET_ENCRYPTION_KEYS="v2:<key>,v1:<key>" (settings.py).
The first one encrypts; every listed one can still decrypt, so a key is rotated by putting the new one
first and keeping the old one until `manage.py encrypt_webhook_secrets` has re-encrypted everything.
A value looks like "enc1:<key id>:<urlsafe base64 of nonce + ciphertext + tag>". The tag also covers the
key id and an `aad` string (the endpoint id and field name), so a value can't be moved to another row.

If the `cryptography` package is approved later, only encrypt/decrypt here need to change; the "enc1" prefix
tells old and new values apart.
"""
import base64
import hashlib
import hmac
import logging
import secrets

from django.conf import settings

logger = logging.getLogger(__name__)

PREFIX = "enc1"
NONCE_BYTES = 16
TAG_BYTES = 32
MIN_KEY_BYTES = 32


class CryptoError(Exception):
    """Wrong key, tampered value or malformed value. Never carries the secret itself."""


def _parse_keys(raw):
    keys = {}
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        key_id, _, material = item.partition(":")
        if not key_id or not material or ":" in key_id:
            raise CryptoError("WEBHOOK_SECRET_ENCRYPTION_KEYS entries must look like <id>:<key>.")
        keys[key_id] = material.encode()
    return keys


def configured_keys():
    """{key id: key material}, active key first. Empty when encryption is not configured."""
    raw = getattr(settings, "WEBHOOK_SECRET_ENCRYPTION_KEYS", "")
    keys = _parse_keys(raw)
    if not keys and settings.DEBUG:
        # Local development only: derive a key so the feature can be tried without setup
        keys = {"dev": hashlib.sha256(b"webhook-secrets:" + settings.SECRET_KEY.encode()).hexdigest().encode()}
    for key_id, material in keys.items():
        if len(material) < MIN_KEY_BYTES:
            raise CryptoError(f"Webhook encryption key '{key_id}' is shorter than {MIN_KEY_BYTES} characters.")
    return keys


def is_enabled():
    return bool(configured_keys())


def active_key_id():
    keys = configured_keys()
    return next(iter(keys)) if keys else ""


def _derive(material, purpose):
    return hmac.new(material, b"tss-webhook-secret|" + purpose, hashlib.sha256).digest()


def _keystream(enc_key, nonce, length):
    out, counter = bytearray(), 0
    while len(out) < length:
        out += hmac.new(enc_key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def _tag(mac_key, key_id, aad, nonce, ciphertext):
    message = b"|".join([PREFIX.encode(), key_id.encode(), aad.encode(), nonce, ciphertext])
    return hmac.new(mac_key, message, hashlib.sha256).digest()


def encrypt(plaintext, aad=""):
    keys = configured_keys()
    if not keys:
        raise CryptoError("No webhook encryption key is configured.")
    key_id, material = next(iter(keys.items()))
    nonce = secrets.token_bytes(NONCE_BYTES)
    data = plaintext.encode()
    ciphertext = bytes(a ^ b for a, b in zip(data, _keystream(_derive(material, b"enc"), nonce, len(data))))
    tag = _tag(_derive(material, b"mac"), key_id, aad, nonce, ciphertext)
    blob = base64.urlsafe_b64encode(nonce + ciphertext + tag).decode()
    return f"{PREFIX}:{key_id}:{blob}"


def key_id_of(value):
    parts = (value or "").split(":", 2)
    return parts[1] if len(parts) == 3 and parts[0] == PREFIX else ""


def decrypt(value, aad=""):
    parts = (value or "").split(":", 2)
    if len(parts) != 3 or parts[0] != PREFIX:
        raise CryptoError("Not an encrypted value.")
    key_id = parts[1]
    material = configured_keys().get(key_id)
    if material is None:
        raise CryptoError(f"No key '{key_id}' is configured.")
    try:
        raw = base64.urlsafe_b64decode(parts[2].encode())
    except ValueError:
        raise CryptoError("Malformed encrypted value.")
    if len(raw) < NONCE_BYTES + TAG_BYTES:
        raise CryptoError("Malformed encrypted value.")
    nonce, ciphertext, tag = raw[:NONCE_BYTES], raw[NONCE_BYTES:-TAG_BYTES], raw[-TAG_BYTES:]
    if not hmac.compare_digest(tag, _tag(_derive(material, b"mac"), key_id, aad, nonce, ciphertext)):
        raise CryptoError("The value failed authentication (wrong key or altered).")
    data = bytes(a ^ b for a, b in zip(ciphertext, _keystream(_derive(material, b"enc"), nonce, len(ciphertext))))
    return data.decode()
