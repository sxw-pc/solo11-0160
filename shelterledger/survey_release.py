"""Review queue for public mock feedback summaries."""

from .store import log


def setup(db):
    db.execute(
        """CREATE TABLE IF NOT EXISTS survey_release(
            round_id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            tally_id TEXT NOT NULL
        )"""
    )


def review(db, round_id, observed, tally_id):
    setup(db)
    state = "publishable" if observed == "minimum_met" else "held"
    db.execute(
        """INSERT INTO survey_release VALUES (?,?,?)
           ON CONFLICT(round_id) DO UPDATE SET
              state=excluded.state, tally_id=excluded.tally_id""",
        (round_id, state, tally_id),
    )
    log(db, "survey-release", round_id, state, "minimum source tally", tally_id)
    return state
