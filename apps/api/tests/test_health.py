import pytest
from fastapi.testclient import TestClient

from app.main import create_app


def test_healthz_reports_ok_and_running_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZIF_APP_VERSION", "1.2.3")

    response = TestClient(create_app()).get("/api/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "1.2.3"}


def test_healthz_version_defaults_to_dev_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZIF_APP_VERSION", raising=False)

    response = TestClient(create_app()).get("/api/healthz")

    assert response.json()["version"] == "dev"
