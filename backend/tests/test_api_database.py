import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text

from app.config import WorkerSettings
from app.db import SessionLocal
from app.main import create_app


def test_the_contract_builds_without_a_database_url() -> None:
    # As `make openapi` runs it: a fresh process, so reading the URL at import time fails too.
    env = {k: v for k, v in os.environ.items() if k != "ZIF_DATABASE_URL"}
    result = subprocess.run(
        [sys.executable, "-m", "app.main"],
        env=env,
        cwd=Path(__file__).parent.parent,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_the_worker_still_requires_a_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZIF_DATABASE_URL")

    with pytest.raises(ValidationError):
        WorkerSettings()


def test_the_api_is_bound_to_its_database_while_it_runs(migrated: None) -> None:
    with TestClient(create_app()):
        with SessionLocal() as session:
            # No statement values (password hashes, emails) in error messages or logs.
            assert session.get_bind().engine.hide_parameters
            # The role from ZIF_DATABASE_URL, not the migrate URL.
            assert session.scalar(text("SELECT current_user")) == "ziftbook_app"
    # Unbound after shutdown, so later code can't use a disposed engine by accident.
    assert SessionLocal.kw.get("bind") is None
