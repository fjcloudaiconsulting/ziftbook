"""ZIF-70: what /api/admin exposes, and to whom."""

import re
import uuid

from fastapi import FastAPI
from fastapi.routing import APIRoute
from sqlalchemy import Engine

from app import admin_auth
from app.main import create_app
from tests.conftest import PASSWORD, People, add_password, email_of, signed_in

PREFIX = "/api/admin"
SIGN_IN = ("POST", "/api/admin/session")


def admin_routes(app: FastAPI) -> set[tuple[str, str]]:
    # include_router keeps an included router's routes behind an _IncludedRouter (as
    # test_names.routes_behind_signed_in walks them); a route added to the app directly is itself.
    routes = [
        route
        for top in app.routes
        for route in (top.original_router.routes if hasattr(top, "original_router") else [top])
    ]
    return {
        (method, route.path)
        for route in routes
        if isinstance(route, APIRoute)
        and (route.path == PREFIX or route.path.startswith(f"{PREFIX}/"))
        for method in route.methods or ()
    }


# fence: an owner, holding a business session, never gets a 2xx (nor a 422 that would show the
# body was looked at) from any /api/admin route, today's or a later one. Kills a route attached to
# a router without the guard.
def test_owner_never_gets_2xx_on_admin_routes(
    people: People, migrate_engine: Engine, bound: None
) -> None:
    add_password(migrate_engine, people.both, PASSWORD)
    app = create_app()
    owner = signed_in(app, people.a, people.both)  # an owner in a
    credentials = {"email": email_of(people.both), "password": PASSWORD}

    answers = {
        (method, path): owner.request(
            method,
            re.sub(r"\{[^}]+\}", str(uuid.uuid4()), path),
            json=credentials if (method, path) == SIGN_IN else {},
        ).status_code
        for method, path in sorted(admin_routes(app))
    }

    assert SIGN_IN in answers
    assert answers == dict.fromkeys(answers, 401)


# fence: /api/admin is exactly sign-in and what a half session needs (admin_half_router). A read
# route goes on admin_auth.admin_router (second factor passed), a write on admin_write_router
# (passed within 15 minutes), never on the half router: widen this list only with that. Kills a
# data route shipped where a password alone reaches it.
def test_admin_routes_are_the_allowlist() -> None:
    assert admin_routes(create_app()) == {
        ("POST", "/api/admin/session"),
        ("GET", "/api/admin/session"),
        ("DELETE", "/api/admin/session"),
        ("POST", "/api/admin/session/second-factor"),
        ("POST", "/api/admin/totp/enrolment"),
        ("POST", "/api/admin/totp/enrolment/confirm"),
    }
    # The password alone reaches only these: a later route on admin_half_router fails here.
    assert {
        (method, route.path)
        for route in admin_auth.admin_half_router.routes
        if isinstance(route, APIRoute)
        for method in route.methods or ()
    } == {
        ("GET", "/api/admin/session"),
        ("DELETE", "/api/admin/session"),
        ("POST", "/api/admin/session/second-factor"),
        ("POST", "/api/admin/totp/enrolment"),
        ("POST", "/api/admin/totp/enrolment/confirm"),
    }
