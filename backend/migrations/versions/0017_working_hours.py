"""working_hours: when each member works, as local times on ISO weekdays; split shifts allowed.

Wall-clock times with no zone: a shift at 09:00 stays at 09:00 when summer time starts or ends.
The app turns them into instants with the business's timezone (app/schedule.py), never the database.

Revision ID: 0017
Revises: 0014
"""

import sqlalchemy as sa
from alembic import op

from app.db import enable_tenant_isolation

revision = "0017"
down_revision = "0014"
branch_labels = None
depends_on = None

OVERLAP = "ex_working_hours_overlap"  # app/schedule.py answers it with 422 overlapping_hours


def upgrade() -> None:
    # Owned by ziftbook_migrate. PUBLIC may use a new type and run its constructor functions by
    # default, so the app role needs no grant. Bounds are '[)': 09:00-12:00 and 12:00-15:00 touch
    # but don't overlap.
    # ponytail: no subtype_diff, so GiST splits are less even; tables stay tiny. Add a
    # time_subtype_diff function (Postgres docs, "Range Types") if the index ever matters.
    op.execute("CREATE TYPE timerange AS RANGE (subtype = time)")
    op.create_table(
        "working_hours",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=False),
        sa.Column("weekday", sa.SmallInteger(), nullable=False),  # ISO: 1 is Monday
        sa.Column("starts_at", sa.Time(), nullable=False),  # time without time zone
        sa.Column("ends_at", sa.Time(), nullable=False),
        sa.CheckConstraint("weekday BETWEEN 1 AND 7", name="iso_weekday"),
        # No overnight shifts: 22:00-02:00 is two rows, on two days.
        sa.CheckConstraint("ends_at > starts_at", name="ends_after_start"),
        # Removing a member removes their hours.
        sa.ForeignKeyConstraint(
            ["tenant_id", "member_id"],
            ["memberships.tenant_id", "memberships.id"],
            ondelete="CASCADE",
        ),
    )
    # Split shifts: any number of rows a day (no UNIQUE (member_id, weekday)), never overlapping.
    # SQL, not ExcludeConstraint: the range is an expression.
    op.execute(f"""
    ALTER TABLE working_hours ADD CONSTRAINT {OVERLAP} EXCLUDE USING gist (
      tenant_id WITH =, member_id WITH =, weekday WITH =, timerange(starts_at, ends_at) WITH &&)
    """)
    enable_tenant_isolation("working_hours")


def downgrade() -> None:
    op.drop_table("working_hours")  # first: its exclusion constraint uses the type
    op.execute("DROP TYPE timerange")  # also drops timemultirange, created with it
