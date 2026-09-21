"""opening_hours: when a business is open, as local times on ISO weekdays; split shifts allowed.

The envelope every member's working hours (0017) and every bookable slot (ZIF-48) must fall inside
(ZIF-105). Wall-clock times with no zone, exactly like working_hours: a shop that opens at 09:00
still opens at 09:00 when summer time starts or ends. The app turns them into instants with the
business's timezone (app/schedule.py), never the database.

No member_id: these are the business's own hours, not a person's. No composite foreign key out of
the table either - it references nothing tenant-owned. Nothing references it *yet*; the
uq_opening_hours_tenant_id_id that enable_tenant_isolation leaves behind (referenced=True, the
default) is the target a future composite foreign key would need, and costs one index until then.

No archived_at and no REVOKE DELETE, because nothing ever points at an opening hour: a week is
replaced wholesale (DELETE + INSERT) on 0001's default privileges. UPDATE is revoked - a wholesale
replace never updates a row.

"Closed every day" is deliberately not expressible here: the API refuses an empty week
(opening_hours_required), so no rows can only ever mean "never configured". A business that wants to
stop taking bookings clears its working hours, archives its services, or blocks the period as time
off (ZIF-101).

Revision ID: 0025
Revises: 0024
"""

import sqlalchemy as sa
from alembic import op

from app.db import enable_tenant_isolation

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

OVERLAP = "ex_opening_hours_overlap"  # app/schedule.py answers it with 422 overlapping_hours


def upgrade() -> None:
    op.create_table(
        "opening_hours",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("weekday", sa.SmallInteger(), nullable=False),  # ISO: 1 is Monday
        sa.Column("starts_at", sa.Time(), nullable=False),  # time without time zone
        sa.Column("ends_at", sa.Time(), nullable=False),
        sa.CheckConstraint("weekday BETWEEN 1 AND 7", name="iso_weekday"),
        # No overnight opening: 22:00-02:00 is two rows, on two days.
        sa.CheckConstraint("ends_at > starts_at", name="ends_after_start"),
    )
    # A lunch closure is two rows a day (no UNIQUE (tenant_id, weekday)), never overlapping. SQL,
    # not ExcludeConstraint: the range is an expression. Bounds are '[)': 09:00-12:00 and
    # 12:00-15:00 touch but don't overlap, and app.schedule.day_shifts joins them back.
    # timerange belongs to migration 0017 and is reused here as is; the downgrade below must never
    # drop it.
    op.execute(f"""
    ALTER TABLE opening_hours ADD CONSTRAINT {OVERLAP} EXCLUDE USING gist (
      tenant_id WITH =, weekday WITH =, timerange(starts_at, ends_at) WITH &&)
    """)
    enable_tenant_isolation("opening_hours")
    # 0001's default privileges granted UPDATE; a week is replaced wholesale, never updated.
    op.execute("REVOKE UPDATE ON opening_hours FROM ziftbook_app")


def downgrade() -> None:
    # Never DROP TYPE timerange here: it belongs to 0017, and working_hours' own exclusion
    # constraint still uses it.
    op.drop_table("opening_hours")
