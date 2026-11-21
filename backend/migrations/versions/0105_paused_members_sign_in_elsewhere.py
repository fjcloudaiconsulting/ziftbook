"""Sign-in picks a business where the person still has a place (ZIF-78).

account_by_email changes in its stored definition only (pg_get_functiondef), one anchored fragment,
as CONTRIBUTING asks: the oldest membership it picks is the oldest of a business that is not
suspended and where the membership is not paused past the plan's places (0104's access_paused_at),
falling back to one that is only when that is all the person has, so sign-in refuses them with a
clear answer. A person paused in one business still signs in to another.

No table is locked: CREATE OR REPLACE FUNCTION takes none, and keeps the owner and grants.

The downgrade reverses the replacement on the stored definition.

Revision ID: 0105
Revises: 0104
"""

import sqlalchemy as sa
from alembic import op

revision = "0105"
down_revision = "0104"
branch_labels = None
depends_on = None

# 0105's anchored fragment in account_by_email (0099's ordering), as (before, after).
EDIT = (
    "ORDER BY (t.suspended_at IS NOT NULL), m.id LIMIT 1",
    "ORDER BY (t.suspended_at IS NOT NULL), (m.access_paused_at IS NOT NULL), m.id LIMIT 1",
)
DEFINITION = sa.text("SELECT pg_get_functiondef(to_regprocedure('account_by_email(text)'))")


def edit(old: str, new: str) -> None:
    """Applies old -> new to the stored account_by_email; refuses unless old is there once."""
    bind = op.get_bind()
    definition = bind.scalar(DEFINITION)
    if definition is None:
        raise RuntimeError("0105: account_by_email is missing")
    if definition.count(old) != 1:
        raise RuntimeError(f"0105: expected {old!r} once in account_by_email")
    # SQL read back from the catalog, run as it is: no bind-parameter parsing of its text.
    bind.exec_driver_sql(definition.replace(old, new))


def upgrade() -> None:
    edit(*EDIT)


def downgrade() -> None:
    edit(EDIT[1], EDIT[0])
