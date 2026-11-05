"""Storefront endpoints for Settings > Customer privacy: cookie choice, data sharing opt out, marketing confirmation."""
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from html import escape

from apps.tenancy.models import Store

from . import privacy
from .rendering import shop_url


def _cookie(response, name, value, store):
    response.set_cookie(
        name, value, max_age=privacy.CONSENT_COOKIE_AGE, path=f"{shop_url(store)}/",
        samesite="Lax", secure=not settings.DEBUG,
    )


@xframe_options_exempt
def storefront_cookie_consent(request, store_slug):
    """The banner's Accept / Decline links: remember the choice, then go back to the page."""
    store = get_object_or_404(Store, store_slug=store_slug)
    choice = "accept" if request.GET.get("choice") == "accept" else "decline"
    response = HttpResponseRedirect(privacy.safe_next(store, request.GET.get("next")))
    _cookie(response, privacy.consent_cookie(store), choice, store)
    return response


@xframe_options_exempt
@csrf_exempt
def storefront_data_opt_out(request, store_slug):
    """The data sharing opt out page: a shopper asks that their data is not shared or sold."""
    store = get_object_or_404(Store, store_slug=store_slug)
    if store.data_sharing_opt_out == "off":
        raise Http404("Page not found.")
    done = request.COOKIES.get(privacy.opt_out_cookie(store)) == "1"
    if request.method == "POST":
        email = (request.POST.get("email") or "").strip().lower()[:320]
        privacy.record_opt_out(store, email)
        response = HttpResponse(privacy.page(
            store, "Your choice is saved",
            "<p>We won't share or sell your personal data. You can change this by contacting the store.</p>",
        ))
        _cookie(response, privacy.opt_out_cookie(store), "1", store)
        return response
    if done:
        body = "<p>You have opted out of data sharing on this device.</p>"
    else:
        body = (
            f"<p>{escape(store.store_name)} may share some of your data with partners to improve products, advertising "
            "and personalization. Use this form to opt out of that sharing.</p>"
            f'<form method="post" action="{shop_url(store)}/privacy/opt-out">'
            '<label style="display:block;margin:16px 0 6px">Email (optional, so we can apply this to your account)</label>'
            '<input type="email" name="email" maxlength="320" autocomplete="email" '
            'style="width:100%;box-sizing:border-box;padding:10px;border:1px solid #bbb;border-radius:8px">'
            '<button type="submit" style="margin-top:16px;background:#14130F;color:#fff;border:0;padding:10px 18px;'
            'border-radius:8px;font-weight:600;cursor:pointer">Opt out of data sharing</button></form>'
        )
    response = HttpResponse(privacy.page(store, "Data sharing opt out", body))
    response["Cache-Control"] = "no-store"
    return response


@xframe_options_exempt
def storefront_marketing_confirm(request, store_slug, token):
    """Double opt-in: the link in the confirmation email."""
    store = get_object_or_404(Store, store_slug=store_slug)
    ok = privacy.confirm(store, token)
    title, text = (
        ("You're subscribed", "Thanks for confirming. You'll now receive marketing from this store.")
        if ok else ("Link expired", "This confirmation link is invalid or has expired. Please sign up again.")
    )
    response = HttpResponse(privacy.page(store, title, f"<p>{text}</p>"), status=200 if ok else 400)
    response["Cache-Control"] = "no-store"
    return response
