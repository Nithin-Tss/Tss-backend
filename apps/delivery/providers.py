"""
Delivery providers. A DeliveryGateway names one of these by `provider`:

  local      the store's own drivers. No outside calls; the shipment carries a driver and moves
             in_transit -> out_for_delivery -> delivered through tracking events.
  manual     any courier the staff books themselves: they type the tracking number, and an optional
             tracking-link template on the gateway ("https://courier.example/t/{tracking}") gives shoppers a link.
  shiprocket books the parcel through Shiprocket's API and receives its status webhooks.
             NOTE: written from Shiprocket's public API docs and covered by tests with a faked HTTP layer only;
             check it against a real Shiprocket account before relying on it.

To add a courier: subclass Provider, implement what it needs, and add it to REGISTRY.
Credentials are stored encrypted on the gateway (see crypto) and never returned by the API.
"""
import hmac
import json
import logging
import urllib.error
import urllib.request
from datetime import datetime

from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from apps.payments import crypto

logger = logging.getLogger(__name__)

# Statuses a shipment can be in. Providers translate their own wording into these.
IN_TRANSIT, OUT_FOR_DELIVERY, DELIVERED, FAILED, RETURNED = (
    "in_transit", "out_for_delivery", "delivered", "failed", "returned",
)


class CarrierUnavailable(APIException):
    status_code = 502
    default_detail = "We couldn't reach the delivery partner. Please try again in a moment."
    default_code = "carrier_unavailable"


def get_credentials(gateway):
    raw = crypto.decrypt(gateway.credentials_enc)
    return json.loads(raw) if raw else {}


def set_credentials(gateway, credentials):
    gateway.credentials_enc = crypto.encrypt(json.dumps(credentials)) if credentials else ""


class Provider:
    code = ""
    label = ""
    kind = "carrier"
    # What the owner enters to connect: {"name", "label", "secret", "required", "help"}; secrets are never shown again
    fields = ()
    books_parcels = False   # True when create_shipment talks to the courier

    @property
    def credential_fields(self):
        return tuple(f["name"] for f in self.fields)

    def missing_credentials(self, creds):
        """Names of required fields that are not filled in."""
        return [f["name"] for f in self.fields if f.get("required") and not creds.get(f["name"])]

    def create_shipment(self, gateway, fulfillment, order, parcel):
        """Book the parcel. Return {"tracking_number", "provider_shipment_id", "tracking_url", "label_url"} (any may be blank)."""
        return {}

    def tracking_url(self, gateway, tracking_number):
        template = (gateway.metadata or {}).get("tracking_url_template", "")
        return template.replace("{tracking}", tracking_number) if template and tracking_number else ""

    def verify_webhook(self, gateway, request):
        """Raise ValidationError unless the request really comes from the courier."""
        secret = crypto.decrypt(gateway.webhook_secret_enc)
        given = request.headers.get("X-Webhook-Secret", "")
        if not secret or not hmac.compare_digest(secret, given):
            raise ValidationError("Webhook secret does not match.")

    def parse_webhook(self, payload):
        """Return a list of {"tracking_number", "status", "location", "description", "event_at"} (event_at may be None)."""
        events = payload if isinstance(payload, list) else [payload]
        return [
            {
                "tracking_number": str(e.get("tracking_number", "")),
                "status": normalise(e.get("status", "")),
                "location": e.get("location") or None,
                "description": e.get("description") or None,
                "event_at": parse_time(e.get("event_at")),
            }
            for e in events
            if e.get("tracking_number") and normalise(e.get("status", ""))
        ]


def parse_time(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if timezone.is_aware(dt) else timezone.make_aware(dt)


def normalise(status):
    s = str(status or "").strip().lower().replace(" ", "_").replace("-", "_")
    if s in (IN_TRANSIT, OUT_FOR_DELIVERY, DELIVERED, FAILED, RETURNED):
        return s
    if "out_for_delivery" in s:
        return OUT_FOR_DELIVERY
    if s.startswith("delivered"):
        return DELIVERED
    if "rto" in s or "return" in s:
        return RETURNED
    if "undelivered" in s or "failed" in s or "cancel" in s:
        return FAILED
    if any(w in s for w in ("transit", "shipped", "picked", "dispatched", "reached")):
        return IN_TRANSIT
    return ""


class LocalProvider(Provider):
    code, label, kind = "local", "Local delivery (own drivers)", "local"


class ManualProvider(Provider):
    code, label = "manual", "Other courier (enter tracking yourself)"


def _http(method, url, body=None, headers=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed https endpoints)
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        logger.warning("Courier call %s %s failed: %s %s", method, url, exc.code, exc.read()[:300])
        raise ValidationError("The delivery partner rejected the booking. Check the credentials and the address.") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        logger.error("Courier call %s %s failed: %s", method, url, exc)
        raise CarrierUnavailable() from exc


class ShiprocketProvider(Provider):
    code, label = "shiprocket", "Shiprocket"
    books_parcels = True
    default_base = "https://apiv2.shiprocket.in/v1/external"
    fields = (
        {"name": "email", "label": "API user email", "required": True,
         "help": "The Shiprocket API user (Settings > API > Create API User), not your main login."},
        {"name": "password", "label": "API user password", "secret": True, "required": True, "help": ""},
        {"name": "pickup_location", "label": "Pickup location name", "required": True,
         "help": "Exactly as named in Shiprocket > Settings > Pickup Addresses."},
        {"name": "api_token", "label": "API token (optional)", "secret": True,
         "help": "If you already have a token, it is used instead of logging in."},
        {"name": "base_url", "label": "API base URL (optional)", "help": f"Leave blank for live: {default_base}"},
    )
    http = staticmethod(_http)

    def _token(self, creds):
        if creds.get("api_token"):
            return creds["api_token"]
        res = self.http("POST", f"{self._base(creds)}/auth/login", {"email": creds["email"], "password": creds["password"]})
        if not res.get("token"):
            raise ValidationError("Shiprocket refused the login.")
        return res["token"]

    def _base(self, creds):
        return (creds.get("base_url") or self.default_base).rstrip("/")

    def create_shipment(self, gateway, fulfillment, order, parcel):
        creds = get_credentials(gateway)
        base = self._base(creds)
        if self.missing_credentials({**creds, "email": creds.get("email") or creds.get("api_token"),
                                     "password": creds.get("password") or creds.get("api_token")}):
            raise ValidationError({"delivery_gateway": "This delivery partner is missing its Shiprocket credentials."})
        addr = order.shipping_address or {}
        name = " ".join(filter(None, [addr.get("first_name"), addr.get("last_name")])) or "Customer"
        items = [
            {"name": fi.order_item.product_name, "sku": str(fi.order_item_id)[:30], "units": fi.quantity,
             "selling_price": str(fi.order_item.unit_price)}
            for fi in fulfillment.items.select_related("order_item")
        ]
        auth = {"Authorization": f"Bearer {self._token(creds)}"}
        booked = self.http("POST", f"{base}/orders/create/adhoc", {
            "order_id": order.order_number,
            "order_date": timezone.localtime(order.created_at).strftime("%Y-%m-%d %H:%M"),
            "pickup_location": creds["pickup_location"],
            "billing_customer_name": name, "billing_last_name": "",
            "billing_address": addr.get("address_line_1", ""), "billing_address_2": addr.get("address_line_2", ""),
            "billing_city": addr.get("city", ""), "billing_pincode": addr.get("postal_code", ""),
            "billing_state": addr.get("state", ""), "billing_country": addr.get("country", ""),
            "billing_email": order.customer_email or "", "billing_phone": addr.get("phone", ""),
            "shipping_is_billing": True, "order_items": items,
            "payment_method": "Prepaid", "sub_total": str(order.subtotal),
            "length": parcel.get("length", 10), "breadth": parcel.get("breadth", 10),
            "height": parcel.get("height", 10), "weight": parcel.get("weight", 0.5),
        }, auth)
        shipment_id = str(booked.get("shipment_id") or "")
        if not shipment_id:
            raise ValidationError("Shiprocket did not accept the order.")
        awb = self.http("POST", f"{base}/courier/assign/awb", {"shipment_id": shipment_id}, auth)
        code = str(((awb.get("response") or {}).get("data") or {}).get("awb_code") or "")
        return {
            "tracking_number": code, "provider_shipment_id": shipment_id,
            "tracking_url": f"https://shiprocket.co/tracking/{code}" if code else "", "label_url": "",
        }

    def verify_webhook(self, gateway, request):
        # Shiprocket echoes the token the owner typed into its webhook settings in the x-api-key header
        secret = crypto.decrypt(gateway.webhook_secret_enc)
        if not secret or not hmac.compare_digest(secret, request.headers.get("x-api-key", "")):
            raise ValidationError("Webhook secret does not match.")

    def parse_webhook(self, payload):
        awb, status = payload.get("awb"), normalise(payload.get("current_status"))
        if not awb or not status:
            return []
        return [{
            "tracking_number": str(awb), "status": status, "location": payload.get("location") or None,
            "description": payload.get("current_status"), "event_at": parse_time(payload.get("current_timestamp")),
        }]


REGISTRY = {p.code: p() for p in (LocalProvider, ManualProvider, ShiprocketProvider)}


def get_provider(code):
    try:
        return REGISTRY[code]
    except KeyError:
        raise ValidationError({"provider": f"Unknown delivery provider '{code}'."})
