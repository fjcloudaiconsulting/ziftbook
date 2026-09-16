import pytest
from psycopg.errors import CheckViolation, InsufficientPrivilege
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from tests.conftest import People


def test_app_role_creates_tenants_with_uuidv7_ids(app_engine: Engine) -> None:
    with app_engine.connect() as conn:  # rolled back on close
        tenant_id = conn.scalar(text("INSERT INTO tenants (name) VALUES ('Nail bar') RETURNING id"))
    assert tenant_id.version == 7


def test_a_new_business_defaults_to_the_netherlands(app_engine: Engine) -> None:
    with app_engine.connect() as conn:  # rolled back on close
        business = conn.execute(
            text("INSERT INTO tenants (name) VALUES ('Nail bar') RETURNING country, currency")
        ).one()
    assert (business.country, business.currency) == ("NL", "EUR")


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE tenants SET currency = 'USD' WHERE id = :a",
        "UPDATE tenants SET country = 'BR' WHERE id = :a",
        "DELETE FROM tenants WHERE id = :a",
    ],
)
def test_the_app_role_cannot_rewrite_currency_country_or_delete_a_business(
    people: People, app_engine: Engine, statement: str
) -> None:
    with pytest.raises(ProgrammingError) as error, app_engine.begin() as conn:
        conn.execute(text(statement), {"a": people.a})
    assert isinstance(error.value.orig, InsufficientPrivilege)


def test_the_app_role_can_rename_a_business_and_lock_its_row(
    people: People, app_engine: Engine
) -> None:
    with app_engine.connect() as conn:  # rolled back on close
        renamed = conn.execute(
            text("UPDATE tenants SET name = 'renamed' WHERE id = :a"), {"a": people.a}
        )
        assert renamed.rowcount == 1
        locked = conn.scalar(
            text("SELECT id FROM tenants WHERE id = :a FOR NO KEY UPDATE"), {"a": people.a}
        )
    assert locked == people.a


@pytest.mark.parametrize("country", ["nl", "N", "NLD", "NL\n"])
def test_a_business_country_must_be_two_uppercase_letters(app_engine: Engine, country: str) -> None:
    with pytest.raises(IntegrityError) as error, app_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants (name, country, currency) VALUES ('x', :c, 'EUR')"),
            {"c": country},
        )
    assert isinstance(error.value.orig, CheckViolation)


@pytest.mark.parametrize("currency", ["eur", "EU", "EURO", "EUR\n"])
def test_a_business_currency_must_be_three_uppercase_letters(
    app_engine: Engine, currency: str
) -> None:
    with pytest.raises(IntegrityError) as error, app_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants (name, country, currency) VALUES ('x', 'NL', :c)"),
            {"c": currency},
        )
    assert isinstance(error.value.orig, CheckViolation)
