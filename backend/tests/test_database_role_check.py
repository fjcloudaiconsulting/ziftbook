"""ZB-08: the API and the worker log the database role their queries run as. See CONTRIBUTING
"Logging". Never a refusal to start."""

import asyncio
import os
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from app import worker
from app.config import WorkerSettings
from app.db import SessionLocal, database_role
from app.main import create_app
from tests.conftest import new_client

Lines = Callable[[], list[dict[str, Any]]]


def url_for(role: str, password: str) -> str:
    return (
        make_url(os.environ["ZIF_DATABASE_URL"])
        .set(username=role, password=password)
        .render_as_string(hide_password=False)
    )


def start_api(monkeypatch: pytest.MonkeyPatch, role: str, password: str) -> None:
    monkeypatch.setenv("ZIF_DATABASE_URL", url_for(role, password))
    with new_client(create_app()):
        pass


def api_lines(lines: Lines) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    started = [x for x in lines() if x["msg"] == "api started"]
    errors = [x for x in lines() if x["msg"].startswith("api database role")]
    return started, errors


@pytest.mark.usefixtures("migrated")
def test_the_api_on_the_app_role_logs_it_and_no_error(
    log_lines: Lines, monkeypatch: pytest.MonkeyPatch
) -> None:
    start_api(monkeypatch, "ziftbook_app", "ziftbook_app")

    started, errors = api_lines(log_lines)
    assert started[-1]["database_role"] == "ziftbook_app"
    assert errors == []


@pytest.mark.usefixtures("migrated")
@pytest.mark.parametrize("role", ["ziftbook_worker", "ziftbook_migrate"])
def test_the_api_on_another_role_logs_an_error_and_still_starts(
    log_lines: Lines, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    start_api(monkeypatch, role, role)

    started, errors = api_lines(log_lines)
    assert started[-1]["database_role"] == role
    assert [(x["level"], x["database_role"]) for x in errors] == [("ERROR", role)]


@pytest.fixture
def switching_engine(migrated: None) -> Iterator[Engine]:
    """Logs in as ziftbook_worker and switches to ziftbook_app on connect, as a production login
    role does: session_user is the worker, current_user is the app role."""
    engine = create_engine(
        url_for("ziftbook_worker", os.environ.get("ZIF_WORKER_PASSWORD") or "ziftbook_worker"),
        poolclass=NullPool,
    )

    @event.listens_for(engine, "connect")
    def _switch(dbapi_connection: Any, record: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("SET ROLE ziftbook_app")
        cursor.close()
        dbapi_connection.commit()

    yield engine
    engine.dispose()


def test_the_check_reads_current_user_not_session_user(switching_engine: Engine) -> None:
    SessionLocal.configure(bind=switching_engine)
    try:
        with switching_engine.connect() as conn:
            assert conn.exec_driver_sql("SELECT session_user").scalar() == "ziftbook_worker"
        assert database_role() == "ziftbook_app"
    finally:
        SessionLocal.configure(bind=None)


def test_a_failing_check_is_none_and_never_raises() -> None:
    SessionLocal.configure(bind=None)
    assert database_role() is None


def serve_as(engine: Engine, log_lines: Lines, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    async def work(*args: object) -> None:
        return None

    monkeypatch.setattr(worker, "work", work)
    SessionLocal.configure(bind=engine)
    try:
        asyncio.run(worker.serve(WorkerSettings()))
    finally:
        SessionLocal.configure(bind=None)
    return [x for x in log_lines() if x["msg"].startswith("worker database role")]


def test_the_worker_on_the_worker_role_logs_it_and_no_warning(
    log_lines: Lines, monkeypatch: pytest.MonkeyPatch, worker_engine: Engine
) -> None:
    warnings = serve_as(worker_engine, log_lines, monkeypatch)

    started = [x for x in log_lines() if x["msg"] == "worker started"]
    assert started[-1]["database_role"] == "ziftbook_worker"
    assert warnings == []


def test_the_worker_on_the_app_role_logs_a_warning(
    log_lines: Lines, monkeypatch: pytest.MonkeyPatch, app_engine: Engine
) -> None:
    warnings = serve_as(app_engine, log_lines, monkeypatch)

    started = [x for x in log_lines() if x["msg"] == "worker started"]
    assert started[-1]["database_role"] == "ziftbook_app"
    assert [(x["level"], x["database_role"]) for x in warnings] == [("WARNING", "ziftbook_app")]
    assert "sign-up, reset and invite emails fail" in warnings[0]["msg"]
