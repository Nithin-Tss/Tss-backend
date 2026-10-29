"""Keeps webhooks from being aimed at the server's own network."""
import ipaddress
import socket
from urllib.parse import urlparse

from django.conf import settings


class UnsafeURL(ValueError):
    pass


def check_url(url):
    """Raises UnsafeURL unless `url` is http(s) and its host resolves only to public addresses."""
    parts = urlparse(url)
    allowed = ("http", "https") if settings.WEBHOOKS_ALLOW_PRIVATE_URLS else ("https",)
    if parts.scheme not in allowed:
        raise UnsafeURL("The URL must start with https://.")
    if not parts.hostname or parts.username or parts.password:
        raise UnsafeURL("The URL needs a host and no embedded credentials.")
    if settings.WEBHOOKS_ALLOW_PRIVATE_URLS:
        return
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise UnsafeURL("That host name can't be found.")
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise UnsafeURL("The URL points at a private or internal address.")
