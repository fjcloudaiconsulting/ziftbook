from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app.jobs import enqueue


def test_enqueueing_the_same_key_twice_adds_one_job(app_engine: Engine) -> None:
    with Session(app_engine) as session:  # rolled back on close
        assert enqueue(session, "test.noop", "test.noop:once", {"n": 1}) is True
        assert enqueue(session, "test.noop", "test.noop:once", {"n": 2}) is False
        payloads = session.scalars(
            text("SELECT payload FROM jobs WHERE dedupe_key = 'test.noop:once'")
        ).all()
    assert payloads == [{"n": 1}]
