"""A business never finds a platform user it has no relationship with (ZIF-99).

Business A (owner O, signed in) probes for two people it has no relationship with: `bm`, a member of
business B, and `bc`, a platform account that is B's client and a member of nothing. `nobody` is an
address with no account. Every probe asks what A can learn: A may learn nothing that tells the three
apart. Docs: CONTRIBUTING.md, "Client records".

Accepted: the per-address booking_verify rate limit (bookings.py) counts across businesses, so a 429
can reveal recent activity elsewhere. It is not an account-exists signal (architect review)."""

import json
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.db import tenant_context
from app.main import create_app
from tests.conftest import (
    People,
    add_user,
    email_of,
    fresh_email,
    member_id,
    new_client,
    save_setting,
    signed_in,
)
from tests.test_availability_api import assign, new_service, weekdays
from tests.test_bookings_api import at, booking_url, guest_body
from tests.test_clients_db import seed_client
from tests.test_openapi import CONTRACT
from tests.test_working_hours import seed

NAME = "Someone Else"


@dataclass(frozen=True)
class World:
    owner: TestClient  # O, signed in to A
    service: str  # in A, performed by O every day 09:00-17:00
    bm: str  # email of a member of B
    bc: str  # email of a platform account that is only B's client
    bc_id: uuid.UUID
    nobody: str  # an address with no account


@pytest.fixture
def app(people: People) -> FastAPI:
    return create_app()


@pytest.fixture
def world(people: People, app: FastAPI, migrate_engine: Engine) -> World:
    owner = signed_in(app, people.a, people.both)
    save_setting(people.a, "published", True)
    service = new_service(owner)
    seed(people.a, people.both, weekdays("09:00", "17:00"))
    assign(people.a, service, member_id(people.a, people.both))
    bc_id = add_user(migrate_engine, name="Cora Platform")
    seed_client(people.b, name="Cora Platform", email=email_of(bc_id), user_id=bc_id)
    return World(
        owner=owner,
        service=service,
        bm=email_of(people.only_b),
        bc=email_of(bc_id),
        bc_id=bc_id,
        nobody=fresh_email(),
    )


def a_row(migrate_engine: Engine, tenant_id: uuid.UUID, email: str) -> dict[str, Any]:
    """The one client row A holds for this address, as stored."""
    with migrate_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        row = conn.execute(
            text("SELECT name, email, user_id FROM clients WHERE email = :e"), {"e": email}
        ).mappings()
        return dict(row.one())


# 1: FENCE. Wrong impl: a search that UNIONs/joins users or other tenants' clients.
def test_searching_clients_never_finds_a_platform_user(people: People, world: World) -> None:
    for address in (world.bm, world.bc):
        for q in (address, address.split("@")[0], "Cora Platform", "Test Person"):
            response = world.owner.get("/api/clients", params={"q": q})
            assert (response.status_code, response.json()) == (200, []), q


# 2: FENCE. Wrong impl: a widened memberships policy or a members query missing its tenant join. (A
# widened users policy alone does not turn this red: members reads FROM memberships, which is
# tenant-scoped. Probe 8 covers the users policy.)
def test_the_member_list_holds_neither(world: World) -> None:
    listed = world.owner.get("/api/members").json()
    assert {m["email"] for m in listed}.isdisjoint({world.bm, world.bc})


# 3: FENCE. Wrong impl: an existence check via account_by_email or a global users lookup.
def test_an_invite_answers_the_same_for_a_member_a_client_and_nobody(
    people: People, world: World, migrate_engine: Engine
) -> None:
    seen = []
    for address in (world.bm, world.bc, world.nobody):
        response = world.owner.post("/api/invites", json={"email": address})
        with migrate_engine.connect() as conn:
            kind = conn.scalar(
                text("SELECT kind FROM jobs WHERE tenant_id = :t AND payload->>'invite_id' = :i"),
                {"t": people.a, "i": response.json().get("id")},
            )
        seen.append((response.status_code, sorted(response.json()), kind))
    assert seen[0] == seen[1] == seen[2]
    # What the route returns, not only the schema: a new response model carrying a link would pass
    # the InviteOut pin below.
    assert seen[0][:2] == (201, ["email", "expired", "expires_at", "id"])
    # If InviteOut returned the token or link, the owner could call POST /api/invites/lookup with it
    # and read has_account for any address.
    assert set(
        json.loads(CONTRACT.read_text())["components"]["schemas"]["InviteOut"]["properties"]
    ) == {
        "id",
        "email",
        "expires_at",
        "expired",
    }, (
        "InviteOut gained a field. A token or link in it lets the owner read has_account via"
        " /api/invites/lookup. Widening it breaks the ZIF-99 directory ban, see CONTRIBUTING"
        ' "Client records".'
    )


# 4: FENCE. Wrong impl: auto-link, or refuse/merge, on a platform match.
def test_creating_a_client_with_a_platform_address_links_nothing(
    people: People, world: World, migrate_engine: Engine
) -> None:
    seen = []
    for address in (world.bm, world.bc, world.nobody):
        response = world.owner.post("/api/clients", json={"name": NAME, "email": address})
        seen.append((response.status_code, sorted(response.json())))
        row = a_row(migrate_engine, people.a, address)
        assert (row["name"], row["user_id"]) == (NAME, None), address
    assert seen[0] == seen[1] == seen[2]
    assert seen[0][0] == 201


# 5: FENCE. Wrong impl: a merchant-created booking claiming the account.
@pytest.mark.parametrize("who", ["bm", "bc"])
def test_a_console_booking_with_a_platform_address_links_nothing(
    people: People, world: World, migrate_engine: Engine, who: str
) -> None:
    address = getattr(world, who)
    response = world.owner.post(
        "/api/bookings",
        json={
            "service_id": world.service,
            "new_client": {"name": NAME, "email": address},
            "starts_at": at("09:00"),
        },
    )
    assert response.status_code == 201, response.json()
    row = a_row(migrate_engine, people.a, address)
    assert (row["name"], row["user_id"]) == (NAME, None)


# 6: FENCE. Wrong impl: link-by-email, or "account exists, sign in" (the 09-29 ruling).
@pytest.mark.parametrize("who", ["bm", "bc"])
def test_a_guest_booking_with_a_platform_address_answers_as_for_nobody(
    people: People, world: World, app: FastAPI, migrate_engine: Engine, who: str
) -> None:
    url = booking_url(people.a, world.service)
    seen = []
    for clock, address in (("10:00", world.nobody), ("11:00", getattr(world, who))):
        response = new_client(app).post(
            url, json=guest_body(starts_at=at(clock), email=address, name=NAME)
        )
        seen.append((response.status_code, sorted(response.json())))
        row = a_row(migrate_engine, people.a, address)
        assert (row["name"], row["user_id"]) == (NAME, None), address
    assert seen[0] == seen[1]
    assert seen[0][0] == 201


# 7: FENCE. Wrong impl: the users policy widened to OR id IN (SELECT user_id FROM clients).
def test_being_a_proven_client_does_not_widen_what_a_can_read(people: People, world: World) -> None:
    seed_client(people.a, name="Cora Platform", email=world.bc, user_id=world.bc_id)
    with tenant_context(people.a) as session:
        direct = session.execute(
            text("SELECT id FROM users WHERE id = :u"), {"u": world.bc_id}
        ).all()
        joined = session.execute(
            text("SELECT u.id FROM clients c JOIN users u ON u.id = c.user_id")
        ).all()
    assert (direct, joined) == ([], [])


# Read from the database on main (ZIF-99): a tenant sees the users that hold a live membership in
# it, not a removed one (ZB-32, 0055). No INSERT policy since ZB-10 (0048): the app role creates no
# user itself.
USERS_POLICIES = [
    [
        "tenant_members",
        "SELECT",
        "{public}",
        "(id IN ( SELECT memberships.user_id\n   FROM memberships\n"
        "  WHERE (memberships.role <> 'removed'::text)))",
        None,
    ],
]
# Every SECURITY DEFINER function ziftbook_app may EXECUTE, in any schema but the system ones, by
# name and identity arguments. Read from the migrated database.
DEFINERS = [
    "public.accept_invite(p_token_hash bytea, p_password_hash text, p_name text)",
    "public.account_by_email(p_email text)",
    "public.begin_totp_enrolment(p_id_hash bytea, p_ticket_proof bytea, p_secret_ct bytea, p_proof bytea)",  # noqa: E501
    "public.complete_password_reset(p_token_hash bytea, p_password_hash text)",
    "public.complete_setup(p_email text, p_password_hash text, p_name text, p_locale text)",
    "public.complete_sign_up(p_token_hash bytea, p_password_hash text, p_business_name text, p_country text, p_currency text, p_name text)",  # noqa: E501
    "public.confirm_totp_enrolment(p_id_hash bytea, p_proof bytea, p_step bigint, p_code_proofs bytea[])",  # noqa: E501
    "public.email_token_live(p_token_hash bytea, p_purpose text)",
    "public.end_admin_session(p_id_hash bytea)",
    "public.needs_setup()",
    "public.pass_second_factor(p_id_hash bytea, p_proof bytea, p_step bigint)",
    "public.password_unchanged(p_user_id uuid, p_hash text)",
    "public.reset_password(p_token_hash bytea, p_password_hash text)",
    "public.resolve_admin_session(p_id_hash bytea)",
    "public.set_own_name(p_user uuid, p_name text)",
    "public.start_admin_session(p_user uuid, p_password_hash text, p_id_hash bytea, p_ip inet, p_user_agent text)",  # noqa: E501
    "public.start_email_token(p_purpose text, p_email text, p_locale text)",
    "public.stripe_account_tenant(p_account text)",
    "public.totp_ciphertext(p_id_hash bytea)",
    "public.use_recovery_code(p_id_hash bytea, p_code_proof bytea)",
]
WIDENING = ' Widening it breaks the ZIF-99 directory ban, see CONTRIBUTING "Client records".'


# 8: FENCE. Wrong impl: any migration that adds to or widens a users policy.
def test_the_users_policies_are_pinned(migrate_engine: Engine) -> None:
    with migrate_engine.connect() as conn:
        rows = conn.execute(
            text("""
            SELECT policyname, cmd, roles::text, qual, with_check FROM pg_policies
            WHERE schemaname = 'public' AND tablename = 'users' ORDER BY policyname
            """)
        ).all()
    assert [list(r) for r in rows] == USERS_POLICIES, (
        "The users policies changed." + WIDENING + " The qual text is Postgres's own deparse and"
        " can change on a major upgrade."
    )


# 9: FENCE. Wrong impl: a new user_exists(email) / find_user(q) definer function, even a wrapper
# over account_by_email, a SQL-standard body, or a cross-tenant clients definer.
def test_the_definer_functions_the_app_can_execute_are_pinned(migrate_engine: Engine) -> None:
    with migrate_engine.connect() as conn:
        names = conn.scalars(
            text("""
            SELECT format('%s.%s(%s)', n.nspname, p.proname,
                          pg_get_function_identity_arguments(p.oid))
            FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE p.prosecdef AND n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND has_function_privilege('ziftbook_app', p.oid, 'EXECUTE')
            ORDER BY 1
            """)
        ).all()
    assert list(names) == DEFINERS, (
        "Adding a SECURITY DEFINER function the app can call is a deliberate change. Check it"
        ' against the ZIF-99 directory ban (CONTRIBUTING "Client records"), then update this pin.'
    )


# 9b: FENCE. Wrong impl: a migrate-owned view over users. users has no FORCE RLS, so the owner's
# view bypasses its policy.
def test_no_view_the_app_can_read_reads_users(migrate_engine: Engine) -> None:
    with migrate_engine.connect() as conn:
        views = conn.scalars(
            text("""
            WITH RECURSIVE reads(oid) AS (
                SELECT 'public.users'::regclass::oid
                UNION
                SELECT r.ev_class FROM reads
                JOIN pg_depend d ON d.refobjid = reads.oid AND d.refclassid = 'pg_class'::regclass
                    AND d.classid = 'pg_rewrite'::regclass
                JOIN pg_rewrite r ON r.oid = d.objid
                WHERE r.ev_class <> reads.oid
            )
            SELECT c.oid::regclass::text FROM reads JOIN pg_class c ON c.oid = reads.oid
            -- any column: a column-level GRANT reads emails as well as a table-level one
            WHERE c.relkind IN ('v', 'm')
              AND has_any_column_privilege('ziftbook_app', c.oid, 'SELECT')
            ORDER BY 1
            """)
        ).all()
    assert list(views) == [], (
        "A view or materialized view over users is readable by the app. users has no FORCE RLS, so"
        " the view bypasses its policy." + WIDENING
    )


# 10: FENCE. Wrong impl: /api/users?q=, or a has_account badge on ClientOut.
def test_no_route_lists_users_and_a_client_has_no_account_field() -> None:
    spec = json.loads(CONTRACT.read_text())
    assert [p for p in spec["paths"] if "users" in p and not p.startswith("/api/admin")] == [], (
        "A route mentions users." + WIDENING
    )
    assert set(spec["components"]["schemas"]["ClientOut"]["properties"]) == {
        "id",
        "name",
        "email",
        "phone",
        "locale",
        "client_note",
        "internal_note",
        "consents",
    }, "ClientOut changed." + WIDENING
