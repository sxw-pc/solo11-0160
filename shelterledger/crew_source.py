"""Intake for synthetic volunteer team service claims."""

from .crew_credit import reconcile


def capture(db, event):
    minutes = int(event["sheet_minutes"])
    if minutes < 0 or not event.get("witness"):
        raise ValueError("nonnegative witnessed work minutes required")
    db.execute(
        "INSERT INTO crew_window(id,team,minutes) VALUES(?,?,?)",
        (event["id"], event["team"], minutes),
    )
    return reconcile(db, event["team"], int(event["claim_minutes"]), event["id"])
