from django.db import models


class ExchangeRate(models.Model):
    """
    The last-known-good rate for one currency, as units of that currency per 1 USD. Market data, not tenant
    data: it is shared by every store and holds nothing about any of them. Rows are only ever replaced by a
    successful fetch, so a provider outage leaves the previous rates in place.
    """

    code = models.CharField(max_length=3, primary_key=True)
    per_usd = models.DecimalField(max_digits=24, decimal_places=12)
    provider = models.CharField(max_length=30)
    fetched_at = models.DateTimeField()

    class Meta:
        db_table = "currency_exchange_rates"

    def __str__(self):
        return f"{self.code} {self.per_usd}"
