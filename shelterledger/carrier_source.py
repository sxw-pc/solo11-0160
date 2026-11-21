"""Acquisition desk for labels on synthetic pet carriers."""

from .carrier_labels import set_label_result
from .store import log, one


def observe(db, event):
    desk = event["shelter_id"]
    if not one(db, "SELECT id FROM shelter WHERE id=?", (desk,)):
        raise ValueError("carrier desk needs a registered shelter")
    observed = {str(value) for value in event["labels_seen"]}
    if not observed:
        raise ValueError("must carry at least one observed label marker")
    state = "unique" if len(observed) == 1 else "multiple"
    log(db, "carrier-read", event["slot"], state, "desk saw these label markers", event["id"])
    return set_label_result(db, event["slot"], state, event["id"])
