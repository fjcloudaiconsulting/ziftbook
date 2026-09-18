"""A business's own copy of its clients, refreshed on each booking, and the append-only log of the
marketing consent given to it, per purpose (ZIF-99)."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from psycopg.errors import UniqueViolation
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import Row, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import auth, passwords
from app.accounts import printable
from app.auth import CurrentSession
from app.business_settings import Locale
from app.errors import ApiError, Error
from app.services import multiline

Purpose = Literal["marketing_email", "sms", "whatsapp"]
Source = Literal["booking_page", "merchant", "import"]

STRICT = ConfigDict(strict=True, extra="forbid")  # as app/services.py:58

ClientName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
    AfterValidator(printable),
]
Phone = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=40),
    AfterValidator(printable),
]
NoteText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2000),
    AfterValidator(multiline),
]
PolicyVersion = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)
]


CONSENT_TEXTS: dict[str, dict[Purpose, str]] = {
    "2026-09-01": {
        "marketing_email": "Email me offers and news from this business. "
        "I can unsubscribe at any time.",
        "sms": "Text me offers and news from this business. I can opt out at any time.",
        "whatsapp": "Message me on WhatsApp with offers and news from this business. "
        "I can opt out at any time.",
    }
}
# ponytail: one wording per purpose, English. The stored evidence must be the text the person
# actually read, so when the booking page ships translated (ZIF-56) the key becomes
# (version, locale), the caller sends the locale it rendered, and an unknown locale is a 422 -
# exactly as an unknown version is today. Do not add a locale parameter before the page has one.


def texts_for(policy_version: str, purposes: Iterable[Purpose]) -> dict[Purpose, str]:
    """The wording a version published for these purposes, or 422. Never the caller's own text:
    that would make the Art. 7(1) evidence attacker-controlled."""
    texts = CONSENT_TEXTS.get(policy_version)
    # The second check is unreachable while every version publishes all three purposes, and goes
    # live the day one doesn't: purposes are caller-supplied, so a subset version would otherwise
    # turn texts[purpose] into a KeyError - a 500 where this ticket's answer is a 422.
    if texts is None or not set(purposes) <= set(texts):
        raise ApiError(422, "unknown_policy_version")
    return texts


@dataclass(frozen=True)
class Found:
    id: UUID
    phone: str | None
    locale: str | None


FIND_OR_CREATE = text("""
INSERT INTO clients (tenant_id, name, email, phone, locale)
VALUES (current_setting('app.tenant_id')::uuid, :name, :email, :phone, :locale)
ON CONFLICT (tenant_id, email) DO UPDATE SET
  name = excluded.name,
  phone = coalesce(excluded.phone, clients.phone),
  locale = coalesce(excluded.locale, clients.locale)
RETURNING id, phone, locale
""")


def find_or_create(
    db: Session, *, name: str, email: str | None, phone: str | None, locale: str | None
) -> Found:
    """The business's own record for this client, created or refreshed. `email` must already be
    normalised (app.passwords.normalise_email); ZIF-51's request model does that.

    One statement, never SELECT-then-INSERT: two bookings for the same address at the same moment
    would race, and the loser would get a unique violation instead of the client.

    DO UPDATE, not DO NOTHING: DO NOTHING returns no row on conflict, so the caller would have to
    look it up again. The SET list refreshes the merchant's copy on each booking (ZIF-99) and
    names only what the booker just typed: never client_note, internal_note or user_id, which are
    the business's own data and the platform link.

    With email IS NULL there is no conflict target, so every booking with no email creates a new
    row. That is intended: two walk-ins named "Jan" are two clients, and merging them is the
    merchant's job, not a guess we make from a name.

    CALLER'S DUTY (ZIF-51): phone and locale come back as *stored*, because coalesce keeps the old
    value when the booker left the field empty. On the public booking page the requester is not
    authenticated, so echoing Found.phone or Found.locale into the response would hand anyone who
    guesses an address that person's stored phone number. Use them to write the booking, never to
    fill a public answer. For the same reason `name = excluded.name` means a stranger who guesses
    an address can rewrite the merchant's record of that person's name: that is inherent to the
    refresh-per-booking rule (ZIF-99), and the booking POST is rate limited per IP because of it.
    """
    row = db.execute(
        FIND_OR_CREATE, {"name": name, "email": email, "phone": phone, "locale": locale}
    ).one()
    return Found(id=row.id, phone=row.phone, locale=row.locale)


RECORD_CONSENTS = text("""
INSERT INTO consents (tenant_id, client_id, purpose, granted, text_shown, policy_version,
                      source, ip, user_agent)
SELECT current_setting('app.tenant_id')::uuid, :client_id, purpose, granted, text_shown,
       :policy_version, :source, CAST(:ip AS inet), :user_agent
FROM unnest(CAST(:purposes AS text[]), CAST(:granted AS boolean[]), CAST(:texts AS text[]))
     AS given(purpose, granted, text_shown)
""")


def record_consents(
    db: Session,
    *,
    client_id: UUID,
    policy_version: str,
    purposes: dict[Purpose, bool],
    source: Source,
    ip: str | None,
    user_agent: str | None,
) -> None:
    """Append one row per purpose: true is a grant, false a withdrawal. Never an UPDATE - the app
    role has no UPDATE or DELETE on this table, so an in-place edit is a 500, by design.

    THIS IS THE TRUST BOUNDARY for the wording: texts_for() is called here, not by the caller, so
    the guard covers ZIF-51's booking path, which calls this function with no route of ours in
    between. Do not "lift it into the route" and do not add a second call in the route either: one
    call, in here.

    The column list here is exactly the migration's GRANT INSERT list. Writes no audit event: the
    booking page's requester is the data subject, and auth.record stamps the requester's address
    and browser into a table with no foreign keys that outlives erased people. The console route
    records the event itself, where the actor is a member of the business. This function therefore
    takes no Request - that absence is the guarantee, and test 26 asserts it.
    """
    texts = texts_for(policy_version, purposes)
    ordered = sorted(purposes)
    db.execute(
        RECORD_CONSENTS,
        {
            "client_id": client_id,
            "policy_version": policy_version,
            "source": source,
            "ip": ip,
            "user_agent": user_agent,
            "purposes": ordered,
            "granted": [purposes[purpose] for purpose in ordered],
            "texts": [texts[purpose] for purpose in ordered],
        },
    )


CURRENT_CONSENTS = text("""
SELECT DISTINCT ON (client_id, purpose) client_id, purpose, granted, source
FROM consents
WHERE client_id = ANY(CAST(:client_ids AS uuid[]))
ORDER BY client_id, purpose, id DESC
""")


def mailable(granted: bool, source: str) -> bool:
    """An imported consent is never mailable: nobody watched it being given, so it is evidence of a
    spreadsheet, not of consent. A grant recorded on the booking page or by the merchant is."""
    return granted and source != "import"


class ConsentOut(BaseModel):
    granted: bool
    source: str
    mailable: bool


def current_consents(
    db: Session, client_ids: Sequence[UUID]
) -> dict[UUID, dict[Purpose, ConsentOut]]:
    """The newest state per (client, purpose) for these clients: {} for a client with no rows.

    One statement for a whole page, never one query per client. Called by every route and directly
    by the database tests, which have no route to go through.
    """
    rows = db.execute(CURRENT_CONSENTS, {"client_ids": list(client_ids)}).all()
    result: dict[UUID, dict[Purpose, ConsentOut]] = {}
    for row in rows:
        result.setdefault(row.client_id, {})[row.purpose] = ConsentOut(
            granted=row.granted, source=row.source, mailable=mailable(row.granted, row.source)
        )
    return result


class ClientOut(BaseModel):
    id: UUID
    name: str
    email: str | None
    phone: str | None
    locale: str | None
    client_note: str | None
    internal_note: str | None  # console-only; see the /api/public fence in tests/test_openapi.py
    consents: dict[Purpose, ConsentOut]  # only purposes that have a row


def as_clients(
    rows: Sequence[Row[Any]], consents: dict[UUID, dict[Purpose, ConsentOut]]
) -> list[ClientOut]:
    """Client rows plus their consents as the API returns them, in the order given."""
    return [
        ClientOut(
            id=row.id,
            name=row.name,
            email=row.email,
            phone=row.phone,
            locale=row.locale,
            client_note=row.client_note,
            internal_note=row.internal_note,
            consents=consents.get(row.id, {}),
        )
        for row in rows
    ]


def like(query: str) -> str:
    """A typed query as a contains-pattern, with LIKE's own metacharacters made literal: a search
    for "a_b" must not match "axb", and "100%" must not match everything."""
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# ponytail: ILIKE with a leading % cannot use an index, so a search scans this business's clients.
# Fine into the thousands. Add pg_trgm and a GIN index on (name, email, phone) if one business
# grows past that; nothing else in the route changes.

FIELDS = "id, name, email, phone, locale, client_note, internal_note"


class ClientIn(BaseModel):
    # Field by field: nothing a client sends can name a business or a platform account. There is no
    # user_id field here or anywhere else in this module; ZIF-51 sets that column from the booker's
    # own session. tests/test_openapi.py fences it for every future request model too.
    model_config = STRICT
    name: ClientName
    email: passwords.Email | None = None
    phone: Phone | None = None
    locale: Locale | None = None


class NoteChange(BaseModel):
    """Only the fields sent change; null clears a note. Nothing else about a client is editable
    here: name, email, phone and locale are the merchant's copy of what the client typed, refreshed
    by the next booking."""

    model_config = STRICT
    client_note: NoteText | None = None
    internal_note: NoteText | None = None


class ConsentIn(BaseModel):
    """What a member recorded on the client's behalf: the policy version their screen showed, and
    true (granted) or false (withdrawn) per purpose. No text_shown field: the server owns the
    wording, so the evidence cannot be written by whoever sends the request."""

    model_config = STRICT
    policy_version: PolicyVersion
    purposes: Annotated[dict[Purpose, bool], Field(min_length=1)]


router = APIRouter(prefix="/api", tags=["clients"])


@router.get("/clients", name="list", responses={s: {"model": Error} for s in (401, 422)})
def list_clients(
    current: CurrentSession,
    response: Response,
    q: Annotated[str | None, Query(max_length=100)] = None,
    before: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[ClientOut]:
    """This business's clients, newest first, optionally searched by name, email or phone."""
    stripped = q.strip() if q else None
    rows = current.db.execute(
        text(f"""
        SELECT {FIELDS} FROM clients
        WHERE (CAST(:before AS uuid) IS NULL OR id < :before)
          AND (CAST(:q AS text) IS NULL
               OR name ILIKE :q OR email ILIKE :q OR phone ILIKE :q)
        ORDER BY id DESC  -- uuidv7: newest first
        LIMIT :limit
        """),
        {"before": before, "q": like(stripped) if stripped else None, "limit": limit},
    ).all()
    response.headers["Cache-Control"] = "no-store"
    return as_clients(rows, current_consents(current.db, [row.id for row in rows]))


@router.post(
    "/clients",
    name="create",
    status_code=201,
    responses={s: {"model": Error} for s in (401, 409, 415, 422)},
)
def create_client(
    new: ClientIn, current: CurrentSession, request: Request, response: Response
) -> ClientOut:
    """A plain insert, never the upsert: the console asking to create a client is not the booking
    page refreshing one. A merchant who names an email already on file is told so (409), rather
    than having that client's name silently overwritten."""
    try:
        row = current.db.execute(
            text(f"""
            INSERT INTO clients (tenant_id, name, email, phone, locale)
            VALUES (current_setting('app.tenant_id')::uuid, :name, :email, :phone, :locale)
            RETURNING {FIELDS}
            """),
            {"name": new.name, "email": new.email, "phone": new.phone, "locale": new.locale},
        ).one()
    except IntegrityError as error:
        # Only the email conflict; any other integrity error stays a 500 (app/members.py:135-146).
        if (
            isinstance(error.orig, UniqueViolation)
            and error.orig.diag.constraint_name == "uq_clients_tenant_id_email"
        ):
            raise ApiError(409, "email_taken") from None
        raise
    client = as_clients([row], {})[0]  # a new client has no consents: {} with no query
    auth.record(
        current.db,
        request,
        "client_created",
        actor_user_id=current.user_id,
        target=f"client:{client.id}",
    )
    response.headers["Cache-Control"] = "no-store"
    return client


@router.patch(
    "/clients/{client_id}",
    name="update",
    responses={s: {"model": Error} for s in (401, 404, 415, 422)},
)
def update_client(
    client_id: UUID,
    change: NoteChange,
    current: CurrentSession,
    request: Request,
    response: Response,
) -> ClientOut:
    """Change client_note and/or internal_note. Only the fields sent change; both are bound from
    the merged state (app/services.py:212-223), never from the request model directly, or a PATCH
    of one note would silently null the other."""
    row = current.db.execute(
        text(f"SELECT {FIELDS} FROM clients WHERE id = :id FOR NO KEY UPDATE"), {"id": client_id}
    ).first()
    if row is None:
        raise ApiError(404, "not_found")
    response.headers["Cache-Control"] = "no-store"
    before = {"client_note": row.client_note, "internal_note": row.internal_note}
    sent = change.model_dump(exclude_unset=True)
    changed: dict[str, object] = {
        key: "changed" for key, value in sent.items() if before[key] != value
    }
    if not changed:  # an empty body, or the same values: nothing written, nothing recorded
        return as_clients([row], current_consents(current.db, [client_id]))[0]
    after = before | sent
    updated = current.db.execute(
        text(f"""
        UPDATE clients SET client_note = :client_note, internal_note = :internal_note
        WHERE id = :id
        RETURNING {FIELDS}
        """),
        {
            "id": client_id,
            "client_note": after["client_note"],
            "internal_note": after["internal_note"],
        },
    ).one()
    auth.record(
        current.db,
        request,
        "client_changed",
        actor_user_id=current.user_id,
        target=f"client:{client_id}",
        details=changed,  # the field names only, never the note text (app/services.py:121)
    )
    return as_clients([updated], current_consents(current.db, [client_id]))[0]


@router.post(
    "/clients/{client_id}/consents",
    name="record_consents",
    status_code=201,
    responses={s: {"model": Error} for s in (401, 404, 415, 422)},
)
def add_consents(
    client_id: UUID,
    body: ConsentIn,
    current: CurrentSession,
    request: Request,
    response: Response,
) -> ClientOut:
    """Record what a member says a client granted or withdrew, on the client's behalf."""
    row = current.db.execute(
        text(f"SELECT {FIELDS} FROM clients WHERE id = :id"), {"id": client_id}
    ).first()
    if row is None:
        raise ApiError(404, "not_found")
    ip, user_agent = auth.origin(request)
    record_consents(
        current.db,
        client_id=client_id,
        policy_version=body.policy_version,
        purposes=body.purposes,
        source="merchant",
        ip=ip,
        user_agent=user_agent,
    )
    auth.record(
        current.db,
        request,
        "consent_recorded",
        actor_user_id=current.user_id,
        target=f"client:{client_id}",
        # Which purposes the merchant touched, never whether the answer was yes: consents holds
        # that, and it is the one an erasure can reach.
        details={"purposes": sorted(body.purposes)},
    )
    response.headers["Cache-Control"] = "no-store"
    return as_clients([row], current_consents(current.db, [client_id]))[0]
