"""ZIF-133: migration 0040's photo columns, their CHECKs and the placeholder backfill, and no image
bytes anywhere in the schema."""

import json
import os
import re
import uuid
from collections.abc import Callable, Iterator
from urllib.parse import urlencode

import pytest
from alembic import command
from alembic.config import Config
from psycopg.errors import CheckViolation
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from app.db import tenant_context
from tests.conftest import API_DIR, People
from tests.test_bookings_db import seed_service
from tests.test_migrations import migration

BYTEA = text("""
SELECT c.relname || '.' || a.attname
FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND a.attnum > 0 AND NOT a.attisdropped
  AND a.atttypid IN ('bytea'::regtype, 'bytea[]'::regtype, 'oid'::regtype)
ORDER BY 1
""")
LO = text("""
SELECT count(*) FROM pg_attribute a JOIN pg_type t ON t.oid = a.atttypid
WHERE t.typname = 'lo' AND a.attnum > 0 AND NOT a.attisdropped
""")
# Today's bytea columns: credential hashes and one ciphertext, never media. A new one fails the test
# until someone adds it here on purpose (the owner's rule: images never go in the database).
ALLOWED = [
    "admin_sessions.id_hash",
    "booking_holds.secret_hash",
    "booking_holds.token_hash",
    "booking_links.token_hash",
    "bookings.checkout_hash",
    "email_tokens.token_hash",
    "invites.token_hash",
    "second_factors.proof_hash",
    "second_factors.recovery_code_hashes",
    "second_factors.secret_ct",
    "second_factors.ticket_hash",
    "sessions.id_hash",
]


@pytest.fixture
def bytea_table(migrate_engine: Engine) -> Iterator[None]:
    with migrate_engine.begin() as conn:
        conn.execute(text("CREATE TABLE broken_photos (id uuid, photo bytea)"))
    yield
    with migrate_engine.begin() as conn:
        conn.execute(text("DROP TABLE broken_photos"))


def bytea_columns(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        assert conn.scalar(LO) == 0
        return list(conn.scalars(BYTEA))


# F1. fence. Kills: a bytea column for image bytes anywhere in the schema (next test).
def test_no_image_bytes_in_schema(migrate_engine: Engine) -> None:
    assert bytea_columns(migrate_engine) == ALLOWED


def test_no_image_bytes_fence_bites(migrate_engine: Engine, bytea_table: None) -> None:
    assert bytea_columns(migrate_engine) != ALLOWED


def set_photo(tenant: uuid.UUID, service: uuid.UUID, key: str, width: int | None = 10) -> None:
    with tenant_context(tenant) as session:
        session.execute(
            text("""
            UPDATE services SET photo_key = :k, photo_content_type = 'image/webp',
              photo_width = :w, photo_height = 10 WHERE id = :s
            """),
            {"k": key, "s": service, "w": width},
        )


# F2. fence. Kills: a key CHECK without the tenant/service pins, or a looser shape.
@pytest.mark.parametrize(
    "make",
    [
        lambda t, s, o: f"{t}/{s}/aGVsbG8gd29ybGQ=",
        lambda t, s, o: f"{o}/{s}/{uuid.uuid4()}",
        lambda t, s, o: f"{t}/{uuid.uuid4()}/{uuid.uuid4()}",
        lambda t, s, o: f"{t}/{s}/{str(uuid.uuid4()).upper()}",
        lambda t, s, o: f"{t}/{s}/{uuid.uuid4()}/thumb.webp",
    ],
    ids=["base64", "other-tenant", "other-service", "upper", "fourth-segment"],
)
def test_photo_key_check_refuses(
    people: People, make: Callable[[uuid.UUID, uuid.UUID, uuid.UUID], str]
) -> None:
    service = seed_service(people.a)
    with pytest.raises(IntegrityError) as error:
        set_photo(people.a, service, make(people.a, service, people.b))
    assert isinstance(error.value.orig, CheckViolation)
    assert "ck_services_photo_key" in str(error.value)


def test_photo_key_valid_inserts(people: People) -> None:
    service = seed_service(people.a)
    set_photo(people.a, service, f"{people.a}/{service}/{uuid.uuid4()}")


# F3. fence. Kills: dropping ck_services_photo_whole, or leaving any one column out of it.
@pytest.mark.parametrize(
    "column", ["photo_key", "photo_content_type", "photo_width", "photo_height"]
)
def test_photo_whole_check(people: People, column: str) -> None:
    service = seed_service(people.a)
    set_photo(people.a, service, f"{people.a}/{service}/{uuid.uuid4()}")
    with pytest.raises(IntegrityError) as error:
        with tenant_context(people.a) as session:
            session.execute(
                text(f"UPDATE services SET {column} = NULL WHERE id = :s"), {"s": service}
            )
    assert "ck_services_photo_whole" in str(error.value)


def migrate_config(monkeypatch: pytest.MonkeyPatch) -> Config:
    url = os.environ["ZIF_MIGRATE_DATABASE_URL"]
    options = urlencode({"options": "-c lock_timeout=5s"})
    monkeypatch.setenv("ZIF_MIGRATE_DATABASE_URL", f"{url}{'&' if '?' in url else '?'}{options}")
    return Config(toml_file=str(API_DIR / "pyproject.toml"))


NAMES = [
    ({"en": "Gel removal"}, "nails"),
    ({"nl": "Knippen"}, "hair"),
    ({"pt": "Depilação"}, "beauty"),
    ({"pt": "DEPILAÇÃO"}, "beauty"),
    ({"pt": "Depila\u0063\u0327\u0061\u0303o"}, "beauty"),  # decomposed (NFD)
    ({"en": "Hand massage"}, "massage"),
    ({"en": "Hair massage"}, "hair"),
    ({"en": "Consultation"}, "generic"),
    ({"nl": "Bagel"}, "generic"),
    ({"en": "2gel"}, "generic"),  # a digit is a word character, as in the web app's rule
    ({"en": "Bridal make-up"}, "beauty"),
    ({"pt": "Limpeza de pele"}, "beauty"),
    ({"en": "Wimper lift", "nl": "Manicure"}, "nails"),
    # Real salon names (frontend/tests/placeholders.test.mjs runs the same table): a short keyword
    # never matches the start of an unrelated word, and facial words are beauty.
    ({"en": "Manicure"}, "nails"),
    ({"en": "Gel manicure"}, "nails"),
    ({"nl": "Gelnagels"}, "nails"),
    ({"nl": "Gellak"}, "nails"),
    ({"nl": "Nagels lakken"}, "nails"),
    ({"pt": "Unhas de gel"}, "nails"),
    ({"en": "Face treatment"}, "beauty"),
    ({"nl": "Gelaatsbehandeling"}, "beauty"),
    ({"nl": "Gezichtsbehandeling"}, "beauty"),
    ({"pt": "Tratamento de rosto"}, "beauty"),
    ({"pt": "Corte masculino"}, "hair"),
    ({"nl": "Lakens wassen"}, "generic"),
]
PHOTO_COLUMNS = text("""
SELECT column_name FROM information_schema.columns
WHERE table_name = 'services' AND column_name LIKE 'photo%' ORDER BY 1
""")


# F4. fence + G1 guard. Kills: no accent folding, a match anywhere (Bagel), kinds or languages in
# the wrong order; and a downgrade or upgrade that loses a column or the CHECKs.
def test_0040_backfill_and_round_trip(
    people: People, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = migrate_config(monkeypatch)
    try:
        command.downgrade(cfg, "0039")
        with migrate_engine.connect() as conn:
            assert list(conn.execute(PHOTO_COLUMNS)) == []
        ids = []
        with tenant_context(people.a) as session:
            for name, _ in NAMES:
                ids.append(
                    session.scalar(
                        text("""
                        INSERT INTO services (tenant_id, name, price_amount_minor,
                                              price_currency, duration_minutes)
                        VALUES (current_setting('app.tenant_id')::uuid, CAST(:n AS jsonb), 100,
                                'EUR', 30) RETURNING id
                        """),
                        {"n": json.dumps(name)},
                    )
                )
    finally:
        command.upgrade(cfg, "head")
    with migrate_engine.connect() as conn:
        assert [r.column_name for r in conn.execute(PHOTO_COLUMNS)] == [
            "photo_content_type",
            "photo_height",
            "photo_key",
            "photo_placeholder",
            "photo_width",
        ]
    with tenant_context(people.a) as session:
        got = [
            session.scalar(text("SELECT photo_placeholder FROM services WHERE id = :i"), {"i": i})
            for i in ids
        ]
    assert got == [kind for _, kind in NAMES]
    # The CHECK came back too.
    test_photo_whole_check(people, "photo_width")


# Guard: the migration's frozen word lists are the web app's (frontend/lib/placeholders.ts), so the
# backfill and the form's pre-selection agree on every word, not only the ones in NAMES.
def test_the_backfill_words_are_the_web_apps() -> None:
    source = (API_DIR.parent / "frontend" / "lib" / "placeholders.ts").read_text()
    web = {
        kind: re.findall(r'"([^"]+)"', words)
        for kind, words in re.findall(r'\[\s*"(\w+)",\s*\[([^\]]*)\],?\s*\]', source)
    }
    frozen = {
        kind: words.split("|") for kind, words in migration("0040_service_photos.py").KINDS.items()
    }
    assert web == frozen
