"""ZIF-83: a name that renders blank is a 422; free text keeps emoji and punctuation."""

from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from app.accounts import BusinessName, DisplayNameText
from app.clients import ClientName, NoteText, Phone
from app.main import create_app
from app.services import DescriptionText, NameText
from app.time_off import Reason
from tests.conftest import new_client
from tests.test_sign_up import complete

# Hangul fillers (category Lo, so a letter test alone lets them through) and the Braille blank.
GLYPHS = ["ᅟ", "ᅠ", "ㅤ", "ﾠ", "⠀"]
NAMES = [BusinessName, DisplayNameText, NameText, ClientName]
FREE_TEXT = [NoteText, DescriptionText, Reason, Phone]


@pytest.mark.parametrize("kind", NAMES)
@pytest.mark.parametrize("value", [*GLYPHS, *(f"An{g}a" for g in GLYPHS), "́̂", "☕"])
def test_a_name_that_renders_blank_or_has_no_letter_or_digit_is_refused(
    kind: Any, value: str
) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(kind).validate_python(value)


@pytest.mark.parametrize("kind", NAMES)
@pytest.mark.parametrize("value", ["카페 서울", "ร้านกาแฟ", "कॉफ़ी", "Café 1", "José"])
def test_a_name_in_any_script_is_accepted(kind: Any, value: str) -> None:
    assert TypeAdapter(kind).validate_python(value) == value


def test_a_blank_business_name_at_sign_up_is_a_422() -> None:
    response = complete(new_client(create_app()), "x", business="⠀", country="NL")

    assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})
