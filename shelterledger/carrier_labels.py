"""Generated carrier-label sleeve readiness for the shelter desk."""

from .store import log


def setup(db):
    db.execute(
        """CREATE TABLE IF NOT EXISTS carrier_sleeve(
            slot TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            reading_id TEXT NOT NULL
        )"""
    )


def set_label_result(db, slot, observed, reading_id):
    setup(db)
    status = "ready" if observed == "unique" else "needs_capture"
    db.execute(
        """INSERT INTO carrier_sleeve VALUES (?, ?, ?)
           ON CONFLICT(slot) DO UPDATE SET state=excluded.state, reading_id=excluded.reading_id""",
        (slot, status, reading_id),
    )
    log(db, "carrier-sleeve", slot, status, "sleeve readiness follows observed code set", reading_id)
    return status
