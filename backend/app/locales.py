"""The languages ziftbook speaks: the one list on the backend (ZIF-102).

Its own module so that auth (which business_settings imports) can use it too. Adding a language:
CONTRIBUTING.md, "Translations".
"""

from typing import Literal

Locale = Literal["en", "nl", "pt"]
