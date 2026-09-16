"""Where a business is: the currency it charges in, and the timezone and language it starts with."""

from typing import Literal, NamedTuple, get_args

from app.business_settings import Locale

Country = Literal["NL", "PT", "BR", "GB", "US"]


class CountryDefaults(NamedTuple):
    currency: str
    timezone: str
    language: Locale


# ponytail: every currency here has 2 minor units; one with 0 or 3 (JPY, BHD) needs that recorded
# before it is added.
COUNTRIES: dict[Country, CountryDefaults] = {
    "NL": CountryDefaults("EUR", "Europe/Amsterdam", "nl"),
    "PT": CountryDefaults("EUR", "Europe/Lisbon", "pt"),
    "BR": CountryDefaults("BRL", "America/Sao_Paulo", "pt"),
    "GB": CountryDefaults("GBP", "Europe/London", "en"),
    # A starting point only: US time zones span six of them, so the owner changes it in settings.
    "US": CountryDefaults("USD", "America/New_York", "en"),
}
assert COUNTRIES.keys() == set(get_args(Country))  # the Literal is the API's enum
