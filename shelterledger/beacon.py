"""Capture synthetic relay-desk heartbeats and source-probing evidence.

A bare heartbeat only reports the receiver light; once a bench of two
simulated ends is declared for the desk, slotted readings, transmission
requests, interruptions, probe plans and readbacks are recorded so the
true source of a beep (late old answer vs. immediate new answer) can be
told apart.
"""

from .store import log, one
from . import comms_desk


def _desk(db, event):
    desk = event["shelter_id"]
    if not one(db, "SELECT id FROM shelter WHERE id=?", (desk,)):
        raise ValueError("a beacon desk must belong to a registered shelter")
    return desk


def capture(db, event):
    desk = _desk(db, event)
    observed = event["observed"]
    if observed not in {"heard", "missing"}:
        raise ValueError("expected a synthetic heartbeat reading")
    if "slot" in event:
        return comms_desk.record_observation(db, desk, event)
    if comms_desk.has_bench(db, desk):
        raise ValueError("a slotted reading is required once a bench is declared")
    log(db, "beacon", desk, observed, "receiver indicator", event["id"])
    return comms_desk.update(db, desk, observed, event["id"])


def bench(db, event):
    return comms_desk.declare_bench(db, _desk(db, event), event)


def request(db, event):
    return comms_desk.add_request(db, _desk(db, event), event)


def interruption(db, event):
    return comms_desk.add_interruption(db, _desk(db, event), event)


def probe(db, event):
    return comms_desk.plan_probe(db, _desk(db, event), event)


def readback(db, event):
    return comms_desk.apply_readback(db, _desk(db, event), event)
