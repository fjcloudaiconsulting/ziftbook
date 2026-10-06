import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.db import SessionLocal
from app.main import create_app


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
    assert "SECRET" not in response.text


def test_healthz_never_touches_the_database() -> None:
    # No lifespan, so SessionLocal is unbound: any session use would raise.
    assert TestClient(create_app()).get("/api/healthz").status_code == 200
