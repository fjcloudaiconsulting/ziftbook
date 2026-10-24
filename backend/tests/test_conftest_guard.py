import pytest

from tests.conftest import refuse_remote_database


@pytest.mark.parametrize("host", ["db.example.com", "10.0.0.5"])
def test_a_remote_database_is_refused(host: str) -> None:
    with pytest.raises(SystemExit):
        refuse_remote_database(f"postgresql+psycopg://u:p@{host}:5432/ziftbook")


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", "postgres"])
def test_a_local_database_is_allowed(host: str) -> None:
    bracketed = f"[{host}]" if ":" in host else host
    refuse_remote_database(f"postgresql+psycopg://u:p@{bracketed}:5432/ziftbook")


def test_a_remote_database_is_allowed_when_named_in_the_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZIF_TESTS_ALLOW_DB", "ziftbook_scratch")
    refuse_remote_database("postgresql+psycopg://u:p@db.example.com:5432/ziftbook_scratch")
    with pytest.raises(SystemExit):
        refuse_remote_database("postgresql+psycopg://u:p@db.example.com:5432/ziftbook")


def test_the_refusal_does_not_print_the_password() -> None:
    with pytest.raises(SystemExit) as refused:
        refuse_remote_database("postgresql+psycopg://u:hunter2@db.example.com:5432/ziftbook")
    assert "hunter2" not in str(refused.value)


@pytest.mark.parametrize(
    "target",
    [
        "u:p@localhost/ziftbook?host=evil.com",
        "u:p@localhost/ziftbook?hostaddr=10.1.1.1",
        "u:p@localhost.evil.com/ziftbook",
        "u:p@localhost@evil.com/ziftbook",
        "u:p@localhost,evil.com/ziftbook",
        "u:p@localhost/ziftbook?host=localhost,evil.com",
    ],
)
def test_a_remote_target_hidden_by_the_url_shape_is_refused(target: str) -> None:
    with pytest.raises(SystemExit):
        refuse_remote_database(f"postgresql+psycopg://{target}")


@pytest.mark.parametrize(
    "target",
    [
        "u:p@localhost/ziftbook?host=localhost",
        "u:p@localhost/ziftbook?host=/var/run/postgresql",
        "u:p@localhost/ziftbook?hostaddr=127.0.0.1",
    ],
)
def test_a_local_target_given_in_the_query_is_allowed(target: str) -> None:
    refuse_remote_database(f"postgresql+psycopg://{target}")
