"""Capture synthetic relay-desk heartbeat rows."""

from .store import log, one
from .comms_desk import update


def capture(db, event):
    desk = event["shelter_id"]
    if not one(db, "SELECT id FROM shelter WHERE id=?", (desk,)):
        raise ValueError("a beacon desk must belong to a registered shelter")
    observed = event["observed"]
    if observed not in {"heard", "missing"}:
        raise ValueError("expected a synthetic heartbeat reading")
    log(db, "beacon", desk, observed, "receiver indicator", event["id"])
    return update(db, desk, observed, event["id"])
