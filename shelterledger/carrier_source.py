"""Acquisition desk for labels on synthetic pet carriers.

The desk accepts two shapes of input:

* ``label-read`` -- the original independent-position reader, unchanged: a slot
  reports the set of codes visible in one look.
* ``pair-round`` / ``pair-image`` / ``split-start`` / ``split-interrupt`` /
  ``split-receipt`` -- the shared-turntable capture added for the paired empty
  carriers.  These hand over sleeves only when a single on-site witness can
  tie a real physical position to an original image.
"""

from .carrier_labels import set_label_result
from .store import log, one
from . import carrier_pair_capture as pair

PAIR_KINDS = ("pair-round", "pair-image", "split-start", "split-interrupt", "split-receipt")


def observe(db, event):
    if event["kind"] != "label-read":
        return _observe_pair(db, event)
    desk = event["shelter_id"]
    if not one(db, "SELECT id FROM shelter WHERE id=?", (desk,)):
        raise ValueError("carrier desk needs a registered shelter")
    observed = {str(value) for value in event["labels_seen"]}
    if not observed:
        raise ValueError("must carry at least one observed label marker")
    state = "unique" if len(observed) == 1 else "multiple"
    log(db, "carrier-read", event["slot"], state, "desk saw these label markers", event["id"])
    return set_label_result(db, event["slot"], state, event["id"])


def _observe_pair(db, event):
    kind = event["kind"]
    if kind == "pair-round":
        return pair.start_round(db, event)
    if kind == "pair-image":
        return pair.record_image(db, event)
    if kind == "split-start":
        return pair.start_split(db, event)
    if kind == "split-interrupt":
        return pair.interrupt_split(db, event)
    if kind == "split-receipt":
        return pair.record_receipt(db, event)
    raise ValueError("unknown carrier event")
