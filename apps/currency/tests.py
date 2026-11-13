"""
Multi-currency: money maths, formatting, the rate providers and their fallback, the data model and its
backfills, the storefront currency choice and what an order records.
"""
import importlib
import re
from datetime import timedelta
from decimal import Decimal as D
from unittest import mock

from django.apps import apps as django_apps
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.catalog.models import ProductVariant
from apps.catalog.tests import URL, signup
from apps.commerce.models import Cart
from apps.commerce.test_integration import ADDRESS, CommerceIntegrationBase
from apps.currency import money, services, storefront
from apps.currency.models import ExchangeRate
from apps.orders.models import Order
from apps.tenancy.models import Store


class ExponentTests(SimpleTestCase):
    def test_minor_unit_exponents(self):
        self.assertEqual((money.exponent("JPY"), money.exponent("USD"), money.exponent("KWD"), money.exponent("inr")), (0, 2, 3, 2))

    def test_unknown_code_is_rejected(self):
        with self.assertRaises(ValueError):
            money.exponent("ZZZ")
        self.assertFalse(money.is_valid("₹"))

    def test_the_list_has_code_name_and_symbol(self):
        inr = next(c for c in money.all_currencies() if c["code"] == "INR")
        self.assertEqual((inr["symbol"], inr["name"], inr["exponent"]), ("₹", "Indian Rupee", 2))
        self.assertGreater(len(money.all_currencies()), 100)


class MinorUnitTests(SimpleTestCase):
    def test_to_minor_per_currency(self):
        self.assertEqual(money.to_minor("399.00", "INR"), 39900)
        self.assertEqual(money.to_minor(D("399"), "JPY"), 399)  # zero-decimal
        self.assertEqual(money.to_minor("1.234", "KWD"), 1234)  # three-decimal

    def test_rounding_is_half_up_not_bankers(self):
        self.assertEqual(money.to_minor("0.005", "USD"), 1)
        self.assertEqual(money.to_minor("0.015", "USD"), 2)
        self.assertEqual(money.to_minor("1.2345", "KWD"), 1235)
        self.assertEqual(money.to_minor("0.5", "JPY"), 1)

    def test_from_minor_keeps_the_currencys_places(self):
        self.assertEqual(money.from_minor(1234, "KWD"), D("1.234"))
        self.assertEqual(money.from_minor(399, "JPY"), D("399"))
        self.assertEqual(str(money.from_minor(5, "USD")), "0.05")

    def test_floats_are_refused(self):
        with self.assertRaises(TypeError):
            money.to_minor(0.1, "USD")

    def test_no_float_drift(self):
        self.assertEqual(sum(money.to_minor("0.10", "USD") for _ in range(3)), 30)


class ConversionTests(SimpleTestCase):
    def test_inr_to_usd(self):
        self.assertEqual(money.convert_minor(39900, "INR", "USD", D("0.01128")), 450)

    def test_into_zero_decimal(self):
        self.assertEqual(money.convert_minor(39900, "INR", "JPY", D("1.8")), 718)  # 399 INR = 718.2 JPY

    def test_into_and_out_of_three_decimal(self):
        self.assertEqual(money.convert_minor(10000, "USD", "KWD", D("0.307")), 30700)  # $100 = 30.700 KWD
        self.assertEqual(money.convert_minor(30700, "KWD", "USD", D("3.2573")), 10000)

    def test_same_currency_is_untouched(self):
        self.assertEqual(money.convert_minor(12345, "INR", "INR", D("1")), 12345)

    def test_markup_is_a_percentage_on_top(self):
        self.assertEqual(money.convert_minor(10000, "USD", "EUR", D("0.9"), markup_percent=D("2")), 9180)


class FormatPriceTests(SimpleTestCase):
    def test_converted_and_formatted(self):
        self.assertEqual(money.format_price(39900, "INR", "USD", "en_US", rate=D("0.01128")), "$4.50")

    def test_grouping_follows_the_locale(self):
        self.assertEqual(money.format_price(10000000, "INR", "INR", "en_IN"), "₹1,00,000.00")
        self.assertEqual(money.format_price(100000, "USD", "USD", "en-US"), "$1,000.00")

    def test_decimals_follow_the_currency(self):
        self.assertTrue(money.format_price(399, "JPY", "JPY", "ja_JP").endswith("399"))
        self.assertIn("1.234", money.format_price(1234, "KWD", "KWD", "en"))

    def test_unknown_locale_falls_back_to_english(self):
        self.assertEqual(money.format_price(100, "USD", "USD", "zz_NOPE"), "$1.00")

    def test_a_missing_rate_shows_the_base_price_not_a_wrong_one(self):
        # rate=None and nothing in the rate table: the base currency is shown as it is
        original = services.get_rate
        services.get_rate = lambda *a: None
        try:
            self.assertEqual(money.format_price(39900, "INR", "USD", "en_IN"), "₹399.00")
        finally:
            services.get_rate = original

    def test_none_is_empty(self):
        self.assertEqual(money.format_price(None, "INR", "USD"), "")


STATIC = {"INR": "88.65", "EUR": "0.86", "JPY": "150"}


@override_settings(FX_PROVIDER="static", FX_STATIC_RATES=STATIC, OXR_APP_ID="")
class RefreshTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_refresh_stores_rates_and_usd(self):
        self.assertTrue(services.refresh_rates())
        self.assertEqual(ExchangeRate.objects.get(code="INR").per_usd, D("88.65"))
        self.assertEqual(ExchangeRate.objects.get(code="USD").per_usd, D("1"))

    def test_cross_rate_goes_through_usd(self):
        services.refresh_rates()
        self.assertEqual(services.get_rate("INR", "EUR"), D("0.86") / D("88.65"))
        self.assertEqual(services.get_rate("INR", "INR"), D("1"))

    def test_a_failed_refresh_keeps_the_last_known_good_rates(self):
        services.refresh_rates()
        with override_settings(FX_STATIC_RATES={}):
            self.assertFalse(services.refresh_rates())
        cache.clear()
        self.assertEqual(services.get_rate("USD", "INR"), D("88.65"))

    def test_a_provider_error_is_swallowed_not_raised(self):
        with mock.patch.object(services.StaticProvider, "fetch", side_effect=services.ProviderError("down")):
            self.assertFalse(services.refresh_rates())

    def test_a_partial_response_only_replaces_what_it_has(self):
        services.refresh_rates()
        with override_settings(FX_STATIC_RATES={"EUR": "0.9"}):
            services.refresh_rates()
        cache.clear()
        self.assertEqual(ExchangeRate.objects.get(code="EUR").per_usd, D("0.9"))
        self.assertEqual(ExchangeRate.objects.get(code="JPY").per_usd, D("150"))  # kept

    def test_invalid_and_non_positive_rates_are_dropped(self):
        with override_settings(FX_STATIC_RATES={"EUR": "0.9", "XXQ": "3", "JPY": "0", "INR": "abc"}):
            services.refresh_rates()
        self.assertEqual(set(ExchangeRate.objects.values_list("code", flat=True)), {"EUR", "USD"})

    def test_rates_older_than_the_limit_are_unavailable(self):
        services.refresh_rates()
        ExchangeRate.objects.filter(code="JPY").update(fetched_at=timezone.now() - timedelta(days=30))
        cache.clear()
        self.assertIsNone(services.get_rate("USD", "JPY"))
        self.assertIsNotNone(services.get_rate("USD", "EUR"))

    def test_page_reads_do_not_call_the_provider(self):
        services.refresh_rates()
        cache.clear()
        with mock.patch.object(services.StaticProvider, "fetch", side_effect=AssertionError("provider called")):
            services.get_rate("INR", "EUR")
            services.get_rate("INR", "EUR")


class ProviderTests(TestCase):
    def setUp(self):
        cache.clear()

    @override_settings(FX_PROVIDER="frankfurter", OXR_APP_ID="")
    def test_frankfurter_is_the_default_and_keyless(self):
        with mock.patch.object(services, "_get_json", return_value={"base": "USD", "rates": {"EUR": 0.86, "INR": 88.65}}) as get:
            self.assertTrue(services.refresh_rates())
        self.assertIn("api.frankfurter.dev/v1/latest?base=USD", get.call_args[0][0])
        self.assertEqual(ExchangeRate.objects.get(code="EUR").provider, "frankfurter")
        self.assertFalse(ExchangeRate.objects.filter(code="AED").exists())  # beyond Frankfurter's ~30

    @override_settings(FX_PROVIDER="frankfurter", OXR_APP_ID="secret")
    def test_open_exchange_rates_fills_in_the_rest_when_configured(self):
        def fake(url, timeout=10):
            if "frankfurter" in url:
                return {"rates": {"EUR": 0.86}}
            return {"rates": {"EUR": 0.85, "AED": 3.6725}}

        with mock.patch.object(services, "_get_json", side_effect=fake):
            services.refresh_rates()
        self.assertEqual(ExchangeRate.objects.get(code="EUR").per_usd, D("0.86"))  # primary wins
        self.assertEqual(ExchangeRate.objects.get(code="AED").per_usd, D("3.6725"))

    @override_settings(FX_PROVIDER="oxr", OXR_APP_ID="secret")
    def test_oxr_can_be_selected(self):
        with mock.patch.object(services, "_get_json", return_value={"rates": {"AED": 3.6725}}) as get:
            self.assertTrue(services.refresh_rates())
        self.assertIn("app_id=secret", get.call_args[0][0])

    @override_settings(FX_PROVIDER="oxr", OXR_APP_ID="")
    def test_oxr_without_a_key_fails_cleanly(self):
        self.assertFalse(services.refresh_rates())

    @override_settings(FX_PROVIDER="nope")
    def test_unknown_provider_fails_cleanly(self):
        self.assertFalse(services.refresh_rates())

    @override_settings(FX_PROVIDER="frankfurter", OXR_APP_ID="")
    def test_a_network_error_falls_back_to_stored_rates(self):
        ExchangeRate.objects.create(code="EUR", per_usd=D("0.8"), provider="x", fetched_at=timezone.now())
        with mock.patch.object(services, "_get_json", side_effect=services.ProviderError("timeout")):
            self.assertFalse(services.refresh_rates())
        cache.clear()
        self.assertTrue(services.rate_available("EUR"))


class StoreSetupMixin:
    def make_store(self, email, currency="INR"):
        token, store_id = signup(self.client, email)
        Store.objects.get(pk=store_id)  # exists
        if currency != "INR":
            Store.objects.get(pk=store_id).currency  # noqa: B018
            store = Store.objects.get(pk=store_id)
            store.currency = currency
            store.save()
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + token, HTTP_X_STORE_ID=store_id)
        return store_id

    def make_product(self, price, sku):
        r = self.client.post(URL, {"title": "Vase " + sku, "status": "active", "price": price, "sku": sku}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return ProductVariant.objects.get(product_id=r.json()["id"])


class VariantMinorUnitTests(StoreSetupMixin, APITestCase):
    def test_price_minor_is_written_with_the_decimal_price(self):
        self.make_store("a@example.com")
        v = self.make_product("399.00", "A-1")
        self.assertEqual((v.price, v.price_minor), (D("399.00"), 39900))

    def test_it_follows_the_stores_currency_exponent(self):
        self.make_store("jp@example.com", "JPY")
        v = self.make_product("399", "JP-1")
        self.assertEqual(v.price_minor, 399)  # JPY has no minor unit

    def test_three_decimal_currency(self):
        # The legacy numeric(12,2) column holds two places, so a KWD price is whole fils to the nearest 10
        self.make_store("kw@example.com", "KWD")
        v = self.make_product("1.23", "KW-1")
        self.assertEqual(v.price_minor, 1230)

    def test_compare_at_and_update_stay_in_sync(self):
        self.make_store("c@example.com")
        v = self.make_product("100", "C-1")
        v.price, v.compare_at_price = D("120.50"), D("150")
        v.save()
        v.refresh_from_db()
        self.assertEqual((v.price_minor, v.compare_at_price_minor), (12050, 15000))
        v.price = D("1.00")
        v.save(update_fields=["price"])
        v.refresh_from_db()
        self.assertEqual(v.price_minor, 100)

    def test_changing_the_base_currency_recomputes_minor_units(self):
        store_id = self.make_store("chg@example.com")
        v = self.make_product("399", "CHG-1")
        r = self.client.patch(f"/api/v1/stores/{store_id}/", {"currency": "JPY"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        v.refresh_from_db()
        self.assertEqual(v.price_minor, 399)
        self.assertEqual(Store.objects.get(pk=store_id).base_currency, "JPY")  # mirrors currency


class BackfillTests(StoreSetupMixin, APITestCase):
    def test_base_currency_backfill_defaults_unknown_to_inr(self):
        migration = importlib.import_module("apps.tenancy.migrations.0022_store_multi_currency")
        a = self.make_store("m1@example.com", "USD")
        Store.objects.filter(pk=a).update(base_currency="", enabled_currencies=[])
        b = self.make_store("m2@example.com")
        Store.objects.filter(pk=b).update(currency="₹", base_currency="", enabled_currencies=[])
        c = self.make_store("m3@example.com")
        Store.objects.filter(pk=c).update(currency="", base_currency="", enabled_currencies=[])
        migration.backfill(django_apps, None)
        got = {str(s.pk): (s.currency, s.base_currency, s.enabled_currencies) for s in Store.objects.all()}
        self.assertEqual(got[str(a)], ("USD", "USD", ["USD"]))
        self.assertEqual(got[str(b)], ("INR", "INR", ["INR"]))
        self.assertEqual(got[str(c)], ("INR", "INR", ["INR"]))

    def test_price_minor_backfill_uses_each_stores_exponent(self):
        migration = importlib.import_module("apps.catalog.migrations.0022_variant_price_minor")
        self.make_store("p1@example.com", "JPY")
        jp = self.make_product("399", "BF-JP")
        self.make_store("p2@example.com")
        inr = self.make_product("399.50", "BF-IN")
        ProductVariant.objects.filter(pk__in=[jp.pk, inr.pk]).update(price_minor=None, compare_at_price_minor=None)
        migration.backfill(django_apps, None)
        jp.refresh_from_db()
        inr.refresh_from_db()
        self.assertEqual((jp.price_minor, inr.price_minor), (399, 39950))

    def test_resync_reports_how_many_changed(self):
        store_id = self.make_store("rs@example.com")
        self.make_product("10", "RS-1")
        ProductVariant.objects.update(price_minor=0)
        self.assertEqual(services.resync_store_prices(Store.objects.get(pk=store_id)), 1)


class StoreSettingsApiTests(StoreSetupMixin, APITestCase):
    def test_defaults_keep_existing_stores_unchanged(self):
        store_id = self.make_store("d@example.com")
        data = self.client.get(f"/api/v1/stores/{store_id}/").json()
        self.assertEqual(data["multiCurrencyEnabled"], False)
        self.assertEqual(data["baseCurrency"], "INR")
        self.assertEqual(data["currency"], "INR")

    def test_enabled_currencies_are_validated_and_normalised(self):
        store_id = self.make_store("e@example.com")
        r = self.client.patch(
            f"/api/v1/stores/{store_id}/", {"enabledCurrencies": ["usd", "EUR", "usd"], "multiCurrencyEnabled": True}, format="json"
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["enabledCurrencies"], ["USD", "EUR"])
        r = self.client.patch(f"/api/v1/stores/{store_id}/", {"enabledCurrencies": ["₹"]}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_settings_do_not_leak_between_tenants(self):
        a = self.make_store("ta@example.com")
        self.client.patch(f"/api/v1/stores/{a}/", {"enabledCurrencies": ["USD"], "multiCurrencyEnabled": True}, format="json")
        b = self.make_store("tb@example.com")
        data = self.client.get(f"/api/v1/stores/{b}/").json()
        self.assertEqual((data["multiCurrencyEnabled"], data["enabledCurrencies"]), (False, []))
        # and B cannot read or change A's
        self.assertEqual(self.client.get(f"/api/v1/stores/{a}/").status_code, 404)
        self.assertEqual(self.client.patch(f"/api/v1/stores/{a}/", {"multiCurrencyEnabled": False}, format="json").status_code, 404)
        self.assertTrue(Store.objects.get(pk=a).multi_currency_enabled)


class RateCurrenciesTests(StoreSetupMixin, APITestCase):
    def test_store_settings_list_the_currencies_that_have_a_rate(self):
        cache.clear()
        ExchangeRate.objects.create(code="EUR", per_usd=D("0.86"), provider="t", fetched_at=timezone.now())
        store_id = self.make_store("l@example.com")
        data = self.client.get(f"/api/v1/stores/{store_id}/").json()
        self.assertEqual(data["rateCurrencies"], ["EUR"])


def seed_rates(**per_usd):
    cache.clear()
    for code, value in {"USD": "1", "INR": "88.65", **per_usd}.items():
        ExchangeRate.objects.update_or_create(code=code, defaults={"per_usd": D(value), "provider": "test", "fetched_at": timezone.now()})


class StorefrontCurrencyTests(APITestCase):
    """INR 399 browsed in USD shows about $4.50, never $399."""

    def setUp(self):
        seed_rates()
        token, self.store_id = signup(self.client, "fx@example.com")
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + token, HTTP_X_STORE_ID=self.store_id)
        self.store = Store.objects.get(pk=self.store_id)
        r = self.client.post(URL, {"title": "Vase", "status": "active", "price": "399", "sku": "FX-1"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.shop = APIClientNoAuth()

    def enable(self, *currencies, **extra):
        Store.objects.filter(pk=self.store_id).update(multi_currency_enabled=True, enabled_currencies=list(currencies), **extra)

    def options(self, html):
        form = re.search(r"<form[^>]*data-currency-selector.*?</form>", html, re.S)
        return re.findall(r'<option value="(\w+)"', form.group(0)) if form else []

    def page(self, path="collections/all", **headers):
        return self.shop.get(f"/s/{self.store.store_slug}/{path}", **headers).content.decode()

    def test_off_by_default_nothing_changes_and_no_selector(self):
        html = self.page()
        self.assertIn("₹399.00", html)
        self.assertNotIn("data-currency-selector", html)
        html = self.page("collections/all?currency=USD")  # ignored while the flag is off
        self.assertIn("₹399.00", html)
        self.assertNotIn("tss_currency", str(self.shop.cookies))

    def test_base_currency_is_shown_with_the_selector_when_enabled(self):
        self.enable("USD")
        html = self.page()
        self.assertIn("₹399.00", html)
        self.assertIn("data-currency-selector", html)
        self.assertEqual(self.options(html), ["INR", "USD"])

    def test_choosing_usd_converts_and_is_remembered(self):
        self.enable("USD")
        html = self.page("collections/all?currency=USD")
        self.assertIn("$4.50", html)
        self.assertNotIn("$399", html)
        self.assertIn(f"tss_currency_{self.store.store_slug}", self.shop.cookies)
        self.assertIn("$4.50", self.page())  # the cookie carries it to the next page

    def test_unknown_or_unenabled_choices_are_ignored(self):
        self.enable("USD")
        seed_rates(EUR="0.86")
        for bad in ("ZZZ", "EUR", "<script>"):
            self.assertIn("₹399.00", self.page(f"collections/all?currency={bad}"))

    def test_a_currency_without_a_rate_is_hidden_not_shown_wrong(self):
        self.enable("USD", "AED")  # no AED rate was ever fetched
        html = self.page()
        self.assertEqual(self.options(html), ["INR", "USD"])
        self.assertIn("₹399.00", self.page("collections/all?currency=AED"))

    def test_geo_default_only_when_enabled_for_the_store(self):
        self.enable("USD")
        self.assertIn("$4.50", self.page(HTTP_CF_IPCOUNTRY="US"))
        self.assertIn("₹399.00", self.page(HTTP_CF_IPCOUNTRY="DE"))  # EUR is not enabled here

    def test_markup_is_added_to_converted_prices(self):
        self.enable("USD", fx_markup_percent=D("10"))
        self.assertIn("$4.95", self.page("collections/all?currency=USD"))  # 4.5008 * 1.10

    def test_search_json_follows_the_visitors_currency(self):
        self.enable("USD")
        data = self.shop.get(f"/s/{self.store.store_slug}/search?q=Vase&format=json&currency=USD").json()
        self.assertEqual(data["results"][0]["price_display"], "$4.50")

    def test_selector_keeps_other_query_params(self):
        self.enable("USD")
        html = self.page("collections/all?sort_by=price")
        self.assertIn('<input type="hidden" name="sort_by" value="price">', html)

    def test_tenants_do_not_see_each_others_settings(self):
        self.enable("USD")
        token, other_id = signup(self.client, "other@example.com")
        other = Store.objects.get(pk=other_id)
        self.assertFalse(other.multi_currency_enabled)
        other_html = self.shop.get(f"/s/{other.store_slug}/collections/all?currency=USD").content.decode()
        self.assertNotIn("data-currency-selector", other_html)
        self.assertNotIn(f"tss_currency_{other.store_slug}", self.shop.cookies)


class APIClientNoAuth:
    """A browser: keeps cookies, sends no login."""

    def __new__(cls):
        from django.test import Client

        return Client()


class CheckoutNoticeTests(SimpleTestCase):
    def test_charge_is_in_the_base_currency_with_a_notice(self):
        store = type("S", (), {"currency": "INR", "base_currency": "INR"})()
        self.assertEqual(services.charge_currency(store, "USD")[0], "INR")
        self.assertIn("estimates", services.charge_currency(store, "USD")[1])
        self.assertEqual(services.charge_currency(store, "INR"), ("INR", ""))
        self.assertEqual(services.charge_currency(store, None), ("INR", ""))

    def test_notice_is_added_before_the_pay_button_only_when_converted(self):
        store = type("S", (), {"currency": "INR", "base_currency": "INR"})()
        page = '<form><button type="submit" id="checkout-submit-btn">Pay</button></form>'
        shown = storefront.Display("INR", "USD", D("0.01"), D(0), "en_US")
        out = storefront._checkout_notice(page, store, shown)
        self.assertTrue(out.index("data-currency-notice") < out.index("checkout-submit-btn"))
        same = storefront.Display("INR", "INR", D(1), D(0), "en_IN")
        self.assertEqual(storefront._checkout_notice(page, store, same), page)


class OrderRecordsCurrencyTests(CommerceIntegrationBase):
    def setUp(self):
        super().setUp()
        seed_rates()
        Store.objects.filter(pk=self.store.pk).update(multi_currency_enabled=True, enabled_currencies=["USD"])

    def place_in(self, display):
        self.add(quantity=2)
        Cart.objects.filter(store=self.store).update(display_currency=display)
        r = self.shopper.post(f"{self.base}/checkout/", {"email": "a@b.com", "shipping_address": ADDRESS}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        placed = self.place(r.data["id"])
        self.assertEqual(placed.status_code, 201, placed.data)
        return Order.objects.get(pk=placed.data["id"])

    def test_order_stores_currency_rate_and_base_amount(self):
        order = self.place_in("USD")
        self.assertEqual(order.currency_code, "INR")  # charged in the base currency
        self.assertEqual(order.display_currency, "USD")
        self.assertEqual(order.fx_rate, (D("1") / D("88.65")).quantize(D("1e-12")))
        self.assertEqual((order.base_currency, order.base_amount_minor), ("INR", int(order.total * 100)))

    def test_without_a_display_currency_only_the_base_amount_is_recorded(self):
        order = self.place_in("")
        self.assertEqual((order.display_currency, order.fx_rate), ("", None))
        self.assertEqual(order.base_amount_minor, int(order.total * 100))

    def test_flag_off_ignores_a_stale_display_currency(self):
        Store.objects.filter(pk=self.store.pk).update(multi_currency_enabled=False)
        order = self.place_in("USD")
        self.assertEqual((order.display_currency, order.fx_rate), ("", None))
