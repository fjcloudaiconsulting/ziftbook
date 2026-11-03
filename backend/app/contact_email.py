"""A business's contact email for clients (ZIF-232): an address is used only once its inbox
confirmed it.

The settings key contact_email holds only the confirmed address, the one in effect; the database
lets no runtime role write it (0087). A typed address waits in contact_email_requests (0086),
which only this module and the mailer name, and is used for nothing until its link is confirmed.
"""

from fastapi import Request
from sqlalchemy import text

from app import auth
from app.auth import SignedIn


def change(current: SignedIn, request: Request, email: str | None) -> None:
    """An owner's PUT of contact_email: a new address waits for its confirmation, the one in effect
    stays until then; None clears both."""
    row = current.db.execute(
        text("SELECT * FROM change_contact_email(:e, false, :u)"),
        {"e": email, "u": current.user_id},
    ).one()
    if row.outcome == "requested":
        # No details: nobody has confirmed the address, and it may be a third party's.
        auth.record(
            current.db,
            request,
            "contact_email_requested",
            actor_user_id=current.user_id,
            target="setting:contact_email",
        )
    elif row.outcome == "cleared" and row.previous is not None:
        auth.record(
            current.db,
            request,
            "setting_changed",
            actor_user_id=current.user_id,
            target="setting:contact_email",
            details={"old": row.previous, "new": None},
        )
