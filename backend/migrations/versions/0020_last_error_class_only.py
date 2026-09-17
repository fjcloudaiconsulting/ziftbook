"""jobs.last_error: rewrite existing rows to the error class only (ZIF-93). A stored repr always
starts with the class name followed by '(', so the leading identifier is kept.

jobs is global, not tenant-owned (CONTRIBUTING "Tenant-owned tables"): no per-tenant loop needed.

Revision ID: 0020
Revises: 0019
"""

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    UPDATE jobs SET last_error = substring(last_error from '^[A-Za-z_][A-Za-z0-9_.]*')
    WHERE last_error IS NOT NULL
    """)


def downgrade() -> None:
    pass  # data can't come back: the original repr (and any personal data in it) is gone for good.
