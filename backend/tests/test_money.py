"""ZIF-102: money in an email reads as the web app formats it (frontend/lib/money.ts): the
business's currency, the reader's number format, the currency's own minor units, and its symbol,
else its code. The expected strings are Node 24's Intl.NumberFormat(locale, {style: "currency",
currency, currencyDisplay: "narrowSymbol"}); U+00A0 is the space Intl puts there."""

from typing import get_args

import pytest

from app import currencies, mail, payments
from app.countries import COUNTRIES

NB = " "


# FENCE. Wrong impl: today's "35.00 CHF" (the code after the amount): Intl writes the code first,
# where a symbol would go, then a no-break space.
@pytest.mark.parametrize(
    ("locale", "expected"),
    [("en", f"CHF{NB}35.00"), ("nl", f"CHF{NB}35,00"), ("pt", f"CHF{NB}35,00")],
)
def test_a_currency_without_a_symbol_shows_its_code_as_intl_does(
    monkeypatch: pytest.MonkeyPatch, locale: str, expected: str
) -> None:
    monkeypatch.setitem(currencies.MINOR_UNITS, "CHF", 2)  # not one a business can hold
    assert mail._money(3500, "CHF", locale) == expected


# Guard (ruling 3): the four currencies a business can hold, each with ISO 4217's 2 minor units.
def test_the_currencies_on_offer() -> None:
    assert currencies.MINOR_UNITS == {"BRL": 2, "EUR": 2, "GBP": 2, "USD": 2}
    assert set(get_args(currencies.Currency)) == set(currencies.MINOR_UNITS)


# Guard: every country's starting currency, and every currency Stripe takes a payment in, is one a
# business can hold.
def test_every_country_and_stripe_currency_is_in_the_table() -> None:
    assert {c.currency for c in COUNTRIES.values()} <= set(currencies.MINOR_UNITS)
    assert set(payments.MIN_CHARGE) <= set(currencies.MINOR_UNITS)


# FENCE. Wrong impl: minor_units(code) defaulting to 2 (.get(code, 2)): a currency nobody listed
# would be formatted as if it had cents.
def test_an_unknown_currency_is_an_error_not_two_decimals() -> None:
    with pytest.raises(KeyError):
        currencies.minor_units("XXX")
    with pytest.raises(KeyError):
        mail._money(3500, "XXX", "en")


# FENCE. Wrong impl: _money's `// 100` and `:02d`: a currency with no minor units (JPY) or three
# (BHD) is scaled by 100 and given 2 decimals. Patched in: neither is a currency a business holds.
@pytest.mark.parametrize(
    ("amount", "currency", "units", "locale", "expected"),
    [
        (3500, "JPY", 0, "en", "¥3,500"),
        (3500, "JPY", 0, "nl", f"¥{NB}3.500"),
        (1234500, "BHD", 3, "en", f"BHD{NB}1,234.500"),
        (1234500, "BHD", 3, "pt", f"BHD{NB}1.234,500"),
        (5, "BHD", 3, "en", f"BHD{NB}0.005"),
    ],
)
def test_money_scales_by_the_currency_s_minor_units(
    monkeypatch: pytest.MonkeyPatch,
    amount: int,
    currency: str,
    units: int,
    locale: str,
    expected: str,
) -> None:
    monkeypatch.setitem(currencies.MINOR_UNITS, currency, units)
    monkeypatch.setitem(mail.SYMBOLS, "JPY", "¥")  # Intl's narrow symbol; BHD has none

    assert mail._money(amount, currency, locale) == expected
