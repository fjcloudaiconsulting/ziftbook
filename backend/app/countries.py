"""Where a business is: the currency it charges in, and the timezone and language it starts with."""

from typing import Literal, NamedTuple

from app.business_settings import Locale
from app.currencies import MINOR_UNITS

Country = Literal["NL", "PT", "BR", "GB", "US"]


class CountryDefaults(NamedTuple):
    currency: str
    timezone: str
    language: Locale


COUNTRIES: dict[Country, CountryDefaults] = {
    "NL": CountryDefaults("EUR", "Europe/Amsterdam", "nl"),
    "PT": CountryDefaults("EUR", "Europe/Lisbon", "pt"),
    "BR": CountryDefaults("BRL", "America/Sao_Paulo", "pt"),
    "GB": CountryDefaults("GBP", "Europe/London", "en"),
    # A starting point only: US time zones span six of them, so the owner changes it in settings.
    "US": CountryDefaults("USD", "America/New_York", "en"),
}

# A country's starting currency is one a business can hold (app/currencies.py).
assert {defaults.currency for defaults in COUNTRIES.values()} <= MINOR_UNITS.keys()
