import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text

from app.config import WorkerSettings
from app.db import SessionLocal
from app.main import create_app, openapi_document
from tests.conftest import API_DIR


def test_the_contract_builds_without_a_database_url() -> None:
    # As `make openapi` runs it: a fresh process, so reading the URL at import time fails too.
    env = {k: v for k, v in os.environ.items() if k != "ZIF_DATABASE_URL"}
    result = subprocess.run(
        [sys.executable, "-m", "app.main"], env=env, cwd=API_DIR, capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == openapi_document()


def test_the_worker_still_requires_a_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZIF_DATABASE_URL")

    with pytest.raises(ValidationError):
        WorkerSettings()


def test_a_running_api_is_bound_to_its_database(migrated: None) -> None:
    with TestClient(create_app()):
        with SessionLocal() as session:
            # The role from ZIF_DATABASE_URL, not the migrate URL.
            assert session.scalar(text("SELECT current_user")) == "ziftbook_app"
    SessionLocal.configure(bind=None)
