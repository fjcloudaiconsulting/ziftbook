"""The app role loses the write privileges it never uses (ZB-10).

0001's default privileges gave ziftbook_app full DML on every table, and some was never narrowed.
Sign-up, first-run setup and invites create users, businesses and memberships through SECURITY
DEFINER functions owned by ziftbook_migrate (complete_sign_up, complete_setup, accept_invite); the
mailer inserts an outbox row and marks it sent. So the app role keeps no INSERT on users or tenants
(nor create_users, the users INSERT policy), no UPDATE or DELETE on users, and on email_outbox
UPDATE of only the columns app/mail.py's _outbox and _mark_sent set.

DELETE on memberships stays, for now: removing a member sets role 'removed' only since ZIF-112
(0045), and the release before it still runs DELETE FROM memberships. Revoked here, that release's
member removal would fail while it runs beside this one during a deploy. The revoke is the contract
step, a later migration shipped once no deployed release deletes a membership.

A function ziftbook_migrate creates from now on is executable by nobody else until its migration
grants it: the default EXECUTE to PUBLIC is gone. Every function the app calls already has its own
GRANT EXECUTE to ziftbook_app; trigger functions need none (firing a trigger checks no EXECUTE).

booking_holds and complete_password_reset are left to the migration that drops them.

Revision ID: 0048
Revises: 0047
"""

from alembic import op

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None

# What app/mail.py's _outbox (ON CONFLICT ... DO UPDATE SET subject) and _mark_sent set.
OUTBOX_UPDATABLE = "status, sent_at, subject"


def upgrade() -> None:
    op.execute("REVOKE INSERT, UPDATE, DELETE ON users FROM ziftbook_app")
    op.execute("DROP POLICY create_users ON users")
    op.execute("REVOKE INSERT ON tenants FROM ziftbook_app")
    # The REVOKE runs before the GRANT (CONTRIBUTING.md): revoking a table-level privilege also
    # takes every column grant of it.
    op.execute("REVOKE UPDATE, DELETE ON email_outbox FROM ziftbook_app")
    op.execute(f"GRANT UPDATE ({OUTBOX_UPDATABLE}) ON email_outbox TO ziftbook_app")
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE ziftbook_migrate REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
    )


def downgrade() -> None:
    # Back to what 0047 left: Postgres's built-in default, table-level UPDATE and DELETE on
    # email_outbox (the table-level REVOKE also takes the column grant), and 0001's DML elsewhere.
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE ziftbook_migrate GRANT EXECUTE ON FUNCTIONS TO PUBLIC"
    )
    op.execute("REVOKE UPDATE ON email_outbox FROM ziftbook_app")
    op.execute("GRANT UPDATE, DELETE ON email_outbox TO ziftbook_app")
    op.execute("GRANT INSERT ON tenants TO ziftbook_app")
    op.execute("CREATE POLICY create_users ON users FOR INSERT WITH CHECK (true)")
    op.execute("GRANT INSERT, UPDATE, DELETE ON users TO ziftbook_app")
