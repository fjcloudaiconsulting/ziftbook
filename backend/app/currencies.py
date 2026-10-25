"""The currencies a business can hold, and each one's minor units (ZIF-102).

Amounts are integers in the currency's minor units everywhere (prices, payments, Stripe), so the
number of decimals comes from here, never an assumed 2. The exponents are ISO 4217's. Adding a
currency: CONTRIBUTING.md, "Money".
"""

from typing import Literal

Currency = Literal["BRL", "EUR", "GBP", "USD"]

MINOR_UNITS: dict[str, int] = {"BRL": 2, "EUR": 2, "GBP": 2, "USD": 2}


def minor_units(currency: str) -> int:
    """The currency's ISO 4217 exponent. KeyError for one not listed: never a guessed 2."""
    return MINOR_UNITS[currency]
