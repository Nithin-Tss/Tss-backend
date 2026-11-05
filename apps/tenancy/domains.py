"""Settings > Domains: connect hostnames to a store and check their DNS."""
import re
import socket
from urllib.parse import urlparse

from django.conf import settings
from rest_framework.exceptions import ValidationError

from .models import Store, StoreDomain

HOST_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def clean_host(raw):
    host = (raw or "").strip().lower()
    if "//" in host:
        host = urlparse(host).hostname or ""
    host = host.split("/")[0].split(":")[0].strip(".")
    if not HOST_RE.match(host):
        raise ValidationError({"host": "Enter a valid domain, like shop.example.com."})
    return host


def target_host():
    """The hostname customers' DNS must point at (CNAME) — where this platform is served."""
    explicit = getattr(settings, "STORE_DNS_TARGET", "")
    if explicit:
        return explicit
    base = getattr(settings, "STORE_BASE_DOMAIN", "")
    if base:
        return base
    for h in getattr(settings, "ALLOWED_HOSTS", []):
        if h and not h.startswith(".") and h != "*" and h not in ("localhost", "127.0.0.1"):
            return h
    return ""


def _ips(host):
    try:
        return {i[4][0] for i in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)}
    except OSError:
        return set()


def dns_ok(host):
    """True when `host` resolves to the same address(es) as the platform target."""
    target = target_host()
    if not target:
        return False
    mine, theirs = _ips(host), _ips(target)
    return bool(mine and theirs and mine & theirs)


def sync_primary(store):
    """Make sure the store's built-in domain is listed as a domain row."""
    if store.domain:
        StoreDomain.objects.get_or_create(host=store.domain, defaults={"store": store})


def add_domain(store, raw_host, kind):
    host = clean_host(raw_host)
    if kind not in (StoreDomain.STOREFRONT, StoreDomain.ACCOUNT):
        raise ValidationError({"kind": "Unknown domain type."})
    if StoreDomain.objects.filter(host=host).exists() or Store.objects.filter(domain=host).exclude(pk=store.pk).exists():
        raise ValidationError({"host": "This domain is already connected to a store."})
    sync_primary(store)
    return StoreDomain.objects.create(store=store, host=host, kind=kind)


def serialize(store, d):
    return {
        "id": d.id,
        "host": d.host,
        "kind": d.kind,
        "primary": d.kind == StoreDomain.STOREFRONT and d.host == store.domain,
        "status": "connected" if dns_ok(d.host) or d.host == store.domain and not target_host() else "invalid_dns",
    }


def listing(store):
    sync_primary(store)
    rows = [serialize(store, d) for d in store.domains.all()]
    rows.sort(key=lambda r: not r["primary"])
    return {"target": target_host(), "domains": rows}
