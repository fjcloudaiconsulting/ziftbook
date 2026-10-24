import logging
import threading
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event

from app import main
from app.db import SessionLocal
from app.main import create_app


@pytest.fixture(autouse=True)
def fresh_probe_cache() -> None:
    main.reset_dependencies_probe()


def test_healthz_reports_ok_and_running_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZIF_APP_VERSION", "1.2.3")
    monkeypatch.setenv("ZIF_APP_REVISION", "abc1234")

    response = TestClient(create_app()).get("/api/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "1.2.3", "revision": "abc1234"}


def test_healthz_defaults_to_dev_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZIF_APP_VERSION", raising=False)
    monkeypatch.delenv("ZIF_APP_REVISION", raising=False)

    response = TestClient(create_app()).get("/api/healthz")

    assert response.json()["version"] == "dev"
    assert response.json()["revision"] == "dev"


def test_dependencies_ok_when_the_database_answers(migrated: None) -> None:
    with TestClient(create_app()) as client:
        response = client.get("/api/health/dependencies")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_dependencies_503_with_a_coarse_code_when_the_database_is_down(migrated: None) -> None:
    with TestClient(create_app()) as client:
        SessionLocal.configure(bind=create_engine("postgresql+psycopg://x:SECRET@127.0.0.1:1/x"))
        response = client.get("/api/health/dependencies")

    assert response.status_code == 503
    assert response.json() == {"code": "database_unavailable"}
    assert "127.0.0.1" not in response.text


class _SpySession:
    def __init__(self, real: Any, calls: list[str]) -> None:
        self._real = real
        self._calls = calls

    def __enter__(self) -> Any:
        self._calls.append("session")
        return self._real.__enter__()

    def __exit__(self, *exc: Any) -> Any:
        return self._real.__exit__(*exc)


class _SpyFactory:
    def __init__(self, real: Any, calls: list[str]) -> None:
        self._real = real
        self._calls = calls

    def __call__(self) -> Any:
        return _SpySession(self._real(), self._calls)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def _spy_on_probes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr("app.main.SessionLocal", _SpyFactory(SessionLocal, calls))
    return calls


def test_dependencies_probes_the_database_once_within_the_ttl(
    migrated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _spy_on_probes(monkeypatch)
    with TestClient(create_app()) as client:
        first = client.get("/api/health/dependencies")
        second = client.get("/api/health/dependencies")

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(calls) == 1


def test_dependencies_probes_again_after_the_ttl(
    migrated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _spy_on_probes(monkeypatch)
    now = [1000.0]
    monkeypatch.setattr("app.main._now", lambda: now[0])
    with TestClient(create_app()) as client:
        client.get("/api/health/dependencies")
        now[0] += main.PROBE_TTL + 0.1
        client.get("/api/health/dependencies")

    assert len(calls) == 2


def test_dependencies_caches_unavailable_and_logs_the_error_once(
    migrated: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = _spy_on_probes(monkeypatch)
    with TestClient(create_app()) as client:
        SessionLocal.configure(bind=create_engine("postgresql+psycopg://x:SECRET@127.0.0.1:1/x"))
        with caplog.at_level(logging.ERROR, logger="app.main"):
            first = client.get("/api/health/dependencies")
            second = client.get("/api/health/dependencies")

    assert first.status_code == second.status_code == 503
    assert second.json() == {"code": "database_unavailable"}
    assert len(calls) == 1
    assert len([r for r in caplog.records if r.message == "dependencies unhealthy"]) == 1


def test_dependencies_probe_is_bounded_by_a_statement_timeout(
    migrated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    with TestClient(create_app()) as client:
        engine = SessionLocal.kw["bind"]

        @event.listens_for(engine, "before_cursor_execute")
        def record(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
            seen.append(statement)

        assert client.get("/api/health/dependencies").status_code == 200
        event.remove(engine, "before_cursor_execute", record)

    assert seen == ["SET LOCAL statement_timeout = '2s'", "SELECT 1"]


def test_concurrent_requests_share_one_probe_and_one_error_line(
    migrated: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    threads_n = 8
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    class _Blocked:
        def __enter__(self) -> Any:
            calls.append("session")
            entered.set()
            release.wait(10)
            raise RuntimeError("database down")

        def __exit__(self, *exc: Any) -> None:
            return None

    with TestClient(create_app()) as client:
        patch = pytest.MonkeyPatch()
        patch.setattr("app.main.SessionLocal", lambda: _Blocked())
        statuses: list[int] = []

        def hit() -> None:
            statuses.append(client.get("/api/health/dependencies").status_code)

        with caplog.at_level(logging.ERROR, logger="app.main"):
            threads = [threading.Thread(target=hit) for _ in range(threads_n)]
            threads[0].start()
            assert entered.wait(10)
            for t in threads[1:]:
                t.start()
            for t in threads[1:]:
                t.join(10)
            release.set()
            threads[0].join(10)
        patch.undo()

    assert statuses == [503] * threads_n
    assert len(calls) == 1
    assert len([r for r in caplog.records if r.message == "dependencies unhealthy"]) == 1


def test_a_hung_probe_serves_the_last_result_only_while_it_is_young(
    migrated: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    now = [1000.0]
    monkeypatch.setattr("app.main._now", lambda: now[0])
    entered = threading.Event()
    release = threading.Event()

    class _Hung:
        def __enter__(self) -> Any:
            entered.set()
            release.wait(10)
            raise RuntimeError("database down")

        def __exit__(self, *exc: Any) -> None:
            return None

    with TestClient(create_app()) as client:
        assert client.get("/api/health/dependencies").status_code == 200  # cached "ok" at 1000
        patch = pytest.MonkeyPatch()
        patch.setattr("app.main.SessionLocal", lambda: _Hung())
        now[0] += main.PROBE_TTL + 0.1
        prober = threading.Thread(target=client.get, args=("/api/health/dependencies",))
        with caplog.at_level(logging.ERROR, logger="app.main"):
            prober.start()
            assert entered.wait(10)
            young = client.get("/api/health/dependencies")
            now[0] = 1000.0 + 2 * main.PROBE_TTL
            old = client.get("/api/health/dependencies")
            logged_while_hung = [r for r in caplog.records if r.message == "dependencies unhealthy"]
            release.set()
            prober.join(10)
        patch.undo()

    assert young.status_code == 200
    assert (old.status_code, old.json()) == (503, {"code": "database_unavailable"})
    assert logged_while_hung == []


@pytest.mark.parametrize(
    ("app_url", "errors"),
    [("https://app.example.com", 1), ("http://localhost:3000", 0)],
    ids=["public", "local"],
)
def test_startup_flags_the_turnstile_opt_out_on_a_public_url(
    log_lines: Callable[[], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    app_url: str,
    errors: int,
) -> None:
    monkeypatch.setenv("ZIF_TURNSTILE_DISABLED", "true")
    monkeypatch.delenv("ZIF_TURNSTILE_SECRET", raising=False)
    monkeypatch.setenv("ZIF_APP_URL", app_url)

    with TestClient(create_app()) as client:
        assert client.get("/api/healthz").status_code == 200

    flagged = [line for line in log_lines() if line["msg"] == "turnstile disabled on a public url"]
    assert [line["level"] for line in flagged] == ["ERROR"] * errors
