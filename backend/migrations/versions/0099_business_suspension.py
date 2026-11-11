"""A business can be suspended (ZIF-73 part 2).

suspended_at and suspension_reason are both NULL or both set (a CHECK pairs them, and caps the
reason). Nothing here sets them: the admin routes do, in the next change. The app role may update
just these two columns (pattern: 0013), and the worker inherits its table-level SELECT.

account_by_email picks the oldest membership of a business that is not suspended, falling back to a
suspended one only when that is all the person has, so sign-in refuses them with a clear answer.

The downgrade restores 0045's account_by_email and drops the CHECK and both columns (the grant goes
with them).

Revision ID: 0099
Revises: 0098
"""

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic import op

revision = "0099"
down_revision = "0098"
branch_labels = None
depends_on = None


def _0045() -> ModuleType:
    path = Path(__file__).with_name("0045_membership_removed.py")
    spec = importlib.util.spec_from_file_location("m0045", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def upgrade() -> None:
    op.execute("""
    ALTER TABLE tenants
      ADD COLUMN suspended_at timestamptz,
      ADD COLUMN suspension_reason text,
      ADD CONSTRAINT ck_tenants_suspension CHECK (
        (suspended_at IS NULL) = (suspension_reason IS NULL)
        AND char_length(suspension_reason) <= 500)
    """)
    op.execute("GRANT UPDATE (suspended_at, suspension_reason) ON tenants TO ziftbook_app")
    # 0045's function, except the oldest-membership subquery sorts suspended businesses last.
    old = _0045()
    oldest = (
        "FROM memberships m WHERE m.user_id = u.id" + old.ACTIVE + "\n            ORDER BY m.id"
    )
    op.execute(
        old.account_by_email(old.ACTIVE).replace(
            oldest,
            "FROM memberships m JOIN tenants t ON t.id = m.tenant_id\n"
            "            WHERE m.user_id = u.id" + old.ACTIVE + "\n"
            "            ORDER BY (t.suspended_at IS NOT NULL), m.id",
        )
    )


def downgrade() -> None:
    old = _0045()
    op.execute(old.account_by_email(old.ACTIVE))
    # ZIF-284: lock first, as 0013's downgrade does.
    op.execute("LOCK TABLE tenants IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "ALTER TABLE tenants DROP CONSTRAINT ck_tenants_suspension,"
        " DROP COLUMN suspended_at, DROP COLUMN suspension_reason"
    )
