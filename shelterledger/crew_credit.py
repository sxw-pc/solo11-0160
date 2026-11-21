"""Existing team-level service credit uses the recorded sheet totals."""

from .store import log, one


def reconcile(db, team, claim, evidence_id):
    row = one(db, "SELECT SUM(minutes) AS total FROM crew_window WHERE team=?", (team,))
    total = row["total"] or 0
    state = "ready" if total >= claim else "held"
    db.execute(
        "INSERT INTO crew_credit VALUES(?,?,?,?) ON CONFLICT(team) DO UPDATE SET "
        "claim=excluded.claim, witnessed=excluded.witnessed, state=excluded.state",
        (team, claim, total, state),
    )
    log(db, "crew", team, state, "verified sheet minutes drive service credit", evidence_id)
    return state
