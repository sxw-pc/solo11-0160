"""Current communication desk follow-up derived from captured heartbeat rows."""

from .store import log


def setup(db):
    db.execute(
        """CREATE TABLE IF NOT EXISTS comms_followup(
            shelter_id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            observation_id TEXT NOT NULL
        )"""
    )


def update(db, shelter_id, observation, observation_id):
    setup(db)
    state = "monitor" if observation == "heard" else "request_readback"
    db.execute(
        """INSERT INTO comms_followup VALUES (?, ?, ?)
           ON CONFLICT(shelter_id) DO UPDATE SET
              state=excluded.state, observation_id=excluded.observation_id""",
        (shelter_id, state, observation_id),
    )
    log(db, "comms", shelter_id, state, "latest heartbeat drives follow-up", observation_id)
    return state
