"""ZIF-102: money in an email reads as the web app formats it (frontend/lib/money.ts): the
business's currency, the reader's number format, the currency's own minor units, and its symbol,
else its code. The expected strings are Node 24's Intl.NumberFormat(locale, {style: "currency",
currency, currencyDisplay: "narrowSymbol"}); U+00A0 is the space Intl puts there."""

import pytest

from app import mail

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
    assert mail._money(3500, "CHF", locale) == expected
