"""memberships.display_name: the name clients see when booking, per business.

Pure expand step: nullable with no default, so every existing row reads NULL ("unset") and no
running version of the app is affected. NULL is the only unset value — the empty string is
impossible (the check refuses length 0, the API strips whitespace and refuses what is left empty).

ZIF-51 snapshots this name onto the booking: the column must stay nullable forever and must never
gain NOT NULL, a default or a uniqueness constraint.

Revision ID: 0023
Revises: 0022
"""

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No grant: 0001's default privileges give ziftbook_app table-wide UPDATE, which covers new
    # columns.
    op.add_column("memberships", sa.Column("display_name", sa.Text()))
    # Resolved to ck_memberships_display_name by the naming convention in app/db.py.
    op.create_check_constraint(
        "display_name", "memberships", "char_length(display_name) BETWEEN 1 AND 60"
    )


def downgrade() -> None:
    # op.f: without it the naming convention is applied a second time and the statement asks for
    # ck_memberships_ck_memberships_display_name.
    op.drop_constraint(op.f("ck_memberships_display_name"), "memberships", type_="check")
    op.drop_column("memberships", "display_name")
