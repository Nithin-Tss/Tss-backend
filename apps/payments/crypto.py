"""Encrypt store-supplied secrets (Stripe keys) before they reach the database. The key is derived from SECRET_KEY."""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet():
    digest = hashlib.sha256(f"store-secrets:{settings.SECRET_KEY}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value):
    return _fernet().encrypt(value.encode()).decode() if value else ""


def decrypt(token):
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return ""
