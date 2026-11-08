import asyncio
import logging
import socket
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app import main
from app.db import SessionLocal
from app.main import create_app


@pytest.fixture(autouse=True)
def fresh_probe_cache() -> Iterator[None]:
    main.reset_dependencies_probe()
    yield
    main.reset_dependencies_probe()  # a cached 503 must not reach the worker's next test


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


def _probe_a_silent_database() -> tuple[list[Any], float]:
    # A listener that is never accept()ed: the kernel completes the TCP handshake, then nothing is
    # ever read or written, as with a database host that stopped answering mid-connection.
    silent = socket.create_server(("127.0.0.1", 0))
    result: list[Any] = []
    with TestClient(create_app()) as client:
        port = silent.getsockname()[1]
        SessionLocal.configure(bind=create_engine(f"postgresql+psycopg://x:x@127.0.0.1:{port}/x"))
        started = time.monotonic()
        # In a thread with a join timeout, so a hang fails the test instead of the run.
        caller = threading.Thread(
            target=lambda: result.append(client.get("/api/health/dependencies")), daemon=True
        )
        caller.start()
        caller.join(8)
        elapsed = time.monotonic() - started
        silent.close()  # ends a hung probe (a regression), so the client can shut down
        caller.join(5)
    return result, elapsed


def test_dependencies_503_within_5s_when_the_database_never_answers(migrated: None) -> None:
    # ZIF-160 fence. Kills: a probe with no bound of its own, which waits as long as the OS TCP
    # timeout (minutes) for a database that stopped answering.
    result, elapsed = _probe_a_silent_database()

    assert result, "the probe hung"
    assert elapsed < 5  # the contract the post-deploy smoke relies on
    assert (result[0].status_code, result[0].json()) == (503, {"code": "database_unavailable"})


def test_the_probe_deadline_bounds_every_phase(
    migrated: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # ZIF-160 fence. Kills: a probe bounded only by the driver's timeouts (connect_timeout, or none
    # at all once connected, as with a frozen server whose kernel still acknowledges), not by
    # PROBE_DEADLINE. The connect timeout is set far past the deadline, so only the deadline can
    # answer in time.
    monkeypatch.setattr(main, "PROBE_DEADLINE", 0.5)
    monkeypatch.setattr(main, "PROBE_CONNECT_TIMEOUT", 30)
    with caplog.at_level(logging.ERROR, logger="app.main"):
        result, elapsed = _probe_a_silent_database()

    assert result, "the probe hung"
    assert elapsed < 2
    assert (result[0].status_code, result[0].json()) == (503, {"code": "database_unavailable"})
    assert [r.message for r in caplog.records] == ["dependencies probe timed out"]


def _spy_on_probes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    real = main._database_ok

    async def spy() -> bool:
        calls.append("probe")
        return await real()

    monkeypatch.setattr("app.main._database_ok", spy)
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
    execute = psycopg.AsyncConnection.execute

    async def record(conn: Any, query: Any, *rest: Any, **kwargs: Any) -> Any:
        seen.append(query)
        return await execute(conn, query, *rest, **kwargs)

    monkeypatch.setattr(psycopg.AsyncConnection, "execute", record)
    with TestClient(create_app()) as client:
        assert client.get("/api/health/dependencies").status_code == 200

    assert seen == ["SET statement_timeout = '2s'", "SELECT 1"]


def test_concurrent_requests_share_one_probe_and_one_error_line(
    migrated: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    threads_n = 8
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    async def blocked(conninfo: str) -> None:
        calls.append("probe")
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        raise RuntimeError("database down")

    with TestClient(create_app()) as client:
        patch = pytest.MonkeyPatch()
        patch.setattr("app.main._select_1", blocked)
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

    async def hung(conninfo: str) -> None:
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        raise RuntimeError("database down")

    with TestClient(create_app()) as client:
        assert client.get("/api/health/dependencies").status_code == 200  # cached "ok" at 1000
        patch = pytest.MonkeyPatch()
        patch.setattr("app.main._select_1", hung)
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
            # ZIF-160. The failed probe is cached too: no stale 200 from the "ok" before it.
            after = client.get("/api/health/dependencies")
        patch.undo()

    assert young.status_code == 200
    assert (old.status_code, old.json()) == (503, {"code": "database_unavailable"})
    assert logged_while_hung == []
    assert after.status_code == 503


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
