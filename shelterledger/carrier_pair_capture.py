"""Paired empty-carrier label capture on a shared turntable.

Two empty carrier props sit on one optical turntable.  One photograph can hold
the ``outer`` page in front of a fixed camera together with an
``inner_reflection`` of another plate, so neither the order of labels inside a
frame nor a reflection ever assigns a real physical position.  A sleeve is
printed only from:

* the ``outer`` reading of a left or right fixed camera on a frame whose
  witness binding fixes one turntable stop and orientation for the whole
  round, or
* an anti-reflective envelope receipt, which locks one physical position and
  admits exactly the single sleeve that receipt covers.

The site authorises two model carriers, two capture rounds, one interruption
and four original label images, and only one physically mutating tray step.
An issued sleeve is immutable: a late image may name what stays unconfirmed
but never rewrites an issued code.  A real split ends the original paired
camera source, so a pair can never be back-filled after it.
"""

import json
import re

from .carrier_labels import setup as sleeve_setup
from .store import log, many, one

LANES = ("left", "right")
VIEW_LANES = ("left", "right", "both")
ORIENTATIONS = ("straight", "turned")
PAGE_SIDES = ("outer", "inner_reflection")
MAX_CARRIERS = 2
MAX_ROUNDS = 2
MAX_IMAGES = 4
MAX_SPLITS = 1
MAX_RECEIPTS = 1
MAX_INTERRUPTS = 1


class CaptureError(ValueError):
    """An event contradicts the physical capture rules."""


def setup(db):
    sleeve_setup(db)
    db.execute(
        """CREATE TABLE IF NOT EXISTS carrier_pair_round(
            round_id TEXT PRIMARY KEY,
            mode TEXT NOT NULL,
            state TEXT NOT NULL,
            interruption TEXT,
            witness TEXT NOT NULL,
            binding_orientation TEXT,
            binding_sides TEXT
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS carrier_image(
            image_id TEXT PRIMARY KEY,
            round_id TEXT NOT NULL REFERENCES carrier_pair_round(round_id),
            lane TEXT NOT NULL,
            orientation TEXT,
            position TEXT,
            envelope_serial TEXT,
            readings TEXT NOT NULL,
            late INTEGER NOT NULL DEFAULT 0
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS carrier_code_position(
            code TEXT PRIMARY KEY,
            position TEXT NOT NULL,
            witness TEXT NOT NULL,
            round_id TEXT NOT NULL REFERENCES carrier_pair_round(round_id)
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS carrier_split_attempt(
            attempt_id INTEGER PRIMARY KEY,
            round_id TEXT NOT NULL REFERENCES carrier_pair_round(round_id),
            state TEXT NOT NULL,
            detail TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS carrier_split_receipt(
            attempt_id INTEGER NOT NULL REFERENCES carrier_split_attempt(attempt_id),
            position TEXT NOT NULL,
            code TEXT NOT NULL,
            envelope_serial TEXT NOT NULL,
            witness TEXT NOT NULL,
            PRIMARY KEY (attempt_id, position)
        )"""
    )


# --- small helpers -----------------------------------------------------------

def _reject_unknown(event, allowed):
    extra = set(event) - set(allowed)
    if extra:
        raise CaptureError(f"unsupported capture field(s): {', '.join(sorted(extra))}")


def _count(db, table, where="", args=()):
    return one(db, f"SELECT COUNT(*) AS n FROM {table} {where}", args)["n"]


def _clean(value, label):
    if not str(value or "").strip():
        raise CaptureError(f"{label} is required")
    return str(value)


def _latest_attempt(db):
    return one(db, "SELECT * FROM carrier_split_attempt ORDER BY attempt_id DESC")


def _images(db, round_id):
    return many(db, "SELECT * FROM carrier_image WHERE round_id=? ORDER BY rowid", (round_id,))


def _decode(blob):
    return json.loads(blob)


def _encode(readings):
    return json.dumps(readings, sort_keys=True, separators=(",", ":"))


def _readings(image):
    readings = image["readings"]
    return readings if isinstance(readings, list) else _decode(readings)


def _outer_codes(image):
    return {reading["code"] for reading in _readings(image)
            if reading["page_side"] == "outer"}


def _bound_codes(image):
    """Only an outer reading of a real fixed edge is positional evidence."""
    if image["position"] is None:
        return set()
    return _outer_codes(image)


def _capsules(db, round_id):
    return [{"image_id": image["image_id"], "lane": image["lane"],
             "orientation": image["orientation"], "position": image["position"],
             "envelope_serial": image["envelope_serial"],
             "readings": _readings(image), "late": bool(image["late"])}
            for image in _images(db, round_id)]


def _normalise_readings(readings, image_id):
    if not isinstance(readings, list) or not readings:
        raise CaptureError(f"image {image_id} needs at least one page reading")
    result, seen = [], set()
    for reading in readings:
        if not isinstance(reading, dict) or set(reading) != {"code", "page_side"}:
            raise CaptureError("each reading needs exactly code and page_side")
        if reading["page_side"] not in PAGE_SIDES:
            raise CaptureError("page_side must be outer or inner_reflection")
        code = _clean(reading["code"], "observed code")
        key = (code, reading["page_side"])
        if key in seen:
            raise CaptureError(f"image {image_id} lists {code} as "
                               f"{reading['page_side']} twice")
        seen.add(key)
        result.append({"code": code, "page_side": reading["page_side"]})
    return result


def _normalise_image(image, mode):
    if not isinstance(image, dict):
        raise CaptureError("each image needs its own record")
    required = {"image_id", "lane", "readings"}
    optional = {"orientation", "position", "envelope_serial"}
    if not required <= set(image) or set(image) - required - optional:
        raise CaptureError("images need image_id, lane and readings")
    if image["lane"] not in VIEW_LANES:
        raise CaptureError("image lane must be left, right or both")
    image_id = _clean(image["image_id"], "image_id")
    readings = _normalise_readings(image["readings"], image_id)
    orientation = image.get("orientation")
    if orientation is not None and orientation not in ORIENTATIONS:
        raise CaptureError(f"image {image_id} needs a real turntable stop")
    serial = image.get("envelope_serial")
    if serial is not None:
        serial = _clean(serial, "envelope serial")
    position = image.get("position")
    if position is not None and position not in LANES:
        raise CaptureError("image position must be left or right when stated")
    if mode == "paired_fixed_camera":
        if orientation is None:
            raise CaptureError(f"image {image_id} needs a real turntable stop")
        # A fixed camera physically owns one edge; an overview owns neither.
        expected = image["lane"] if image["lane"] in LANES else None
        if position is not None and position != expected:
            raise CaptureError("a fixed camera cannot name a different physical position")
        position = expected
    return {"image_id": image_id, "lane": image["lane"], "orientation": orientation,
            "position": position, "envelope_serial": serial, "readings": readings}


def _insert_image(db, round_id, capsule):
    db.execute(
        """INSERT INTO carrier_image
           (image_id, round_id, lane, orientation, position, envelope_serial, readings, late)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (capsule["image_id"], round_id, capsule["lane"], capsule["orientation"],
         capsule["position"], capsule["envelope_serial"], _encode(capsule["readings"]),
         int(bool(capsule.get("late")))),
    )


def _one_stop(capsules):
    stops = {capsule["orientation"] for capsule in capsules}
    if len(stops) > 1:
        raise CaptureError("a round must share one turntable stop; two near-matches from "
                           "different stops cannot be mirrored into one witnessed frame")


# --- round intake ------------------------------------------------------------

def _binding(event, round_id, mode):
    if mode == "separate_envelopes":
        return {"witness": "envelope receipt pending", "orientation": None, "sides": None}
    binding = event.get("binding")
    if not isinstance(binding, dict):
        raise CaptureError("a witness binding is required for the same turntable stop")
    if set(binding) != {"witness", "round_id", "orientation", "sides"}:
        raise CaptureError("binding must carry witness, round_id, orientation and sides")
    if binding["round_id"] != round_id:
        raise CaptureError("binding names a different round than the images")
    if binding["orientation"] not in ORIENTATIONS:
        raise CaptureError("binding orientation must be straight or turned")
    if binding["sides"] != "both":
        raise CaptureError("a paired frame must fix both left and right camera sides")
    return {"witness": _clean(binding["witness"], "binding witness"),
            "orientation": binding["orientation"], "sides": binding["sides"]}


def start_round(db, event):
    setup(db)
    _reject_unknown(event, {"id", "kind", "shelter_id", "round_id", "mode", "turntable",
                            "images", "binding"})
    if not one(db, "SELECT id FROM shelter WHERE id=?", (event["shelter_id"],)):
        raise CaptureError("carrier desk needs a registered shelter")
    mode = event.get("mode")
    if mode not in ("paired_fixed_camera", "separate_envelopes"):
        raise CaptureError("mode must be paired_fixed_camera or separate_envelopes")
    if mode == "paired_fixed_camera" and _count(db, "carrier_split_attempt"):
        raise CaptureError("the tray was already split; the paired source cannot be re-filmed")
    if mode == "separate_envelopes" and not _latest_attempt(db):
        raise CaptureError("separate envelopes follow the one authorised physical split")
    if mode == "separate_envelopes" and _latest_attempt(db)["state"] == "interrupted":
        raise CaptureError("split was interrupted; cage positions are no longer known")
    if _count(db, "carrier_pair_round") >= MAX_ROUNDS:
        raise CaptureError(f"at most {MAX_ROUNDS} capture rounds are permitted")
    round_id = event.get("round_id") or f"R{_count(db, 'carrier_pair_round') + 1}"
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", str(round_id)):
        raise CaptureError("round_id must be a simple identifier")
    if one(db, "SELECT round_id FROM carrier_pair_round WHERE round_id=?", (round_id,)):
        raise CaptureError(f"round {round_id} already recorded")
    images = event.get("images")
    if not isinstance(images, list) or not 1 <= len(images) <= MAX_IMAGES:
        raise CaptureError(f"round needs between 1 and {MAX_IMAGES} original images")
    if _count(db, "carrier_image") + len(images) > MAX_IMAGES:
        raise CaptureError(f"at most {MAX_IMAGES} label images may be recorded in total")
    if mode == "separate_envelopes" and _latest_attempt(db)["state"] == "interrupted":
        raise CaptureError("split was interrupted; cage positions are no longer known")
    binding = _binding(event, round_id, mode)
    state, interruption = "recorded", None
    if mode == "paired_fixed_camera":
        turntable = event.get("turntable")
        if not isinstance(turntable, dict) or turntable.get("houses") != MAX_CARRIERS:
            raise CaptureError("paired capture needs exactly two empty carriers on the turntable")
        if not isinstance(turntable.get("shared_stop"), bool):
            raise CaptureError("turntable evidence must say whether both cameras shared one stop")
        if not turntable["shared_stop"]:
            state, interruption = "interrupted", "shared_stop_not_witnessed"
    capsules = [_normalise_image(image, mode) for image in images]
    if len({capsule["image_id"] for capsule in capsules}) != len(capsules):
        raise CaptureError("image ids must be unique")
    for capsule in capsules:
        if one(db, "SELECT image_id FROM carrier_image WHERE image_id=?", (capsule["image_id"],)):
            raise CaptureError("image ids must be unique")
    if mode == "paired_fixed_camera":
        _one_stop(capsules)
        if any(capsule["orientation"] != binding["orientation"] for capsule in capsules):
            raise CaptureError("the witness binding names a different turntable stop than the images")
    db.execute(
        """INSERT INTO carrier_pair_round
           (round_id, mode, state, interruption, witness, binding_orientation, binding_sides)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (round_id, mode, state, interruption, binding["witness"],
         binding["orientation"], binding["sides"]),
    )
    for capsule in capsules:
        _insert_image(db, round_id, capsule)
    log(db, "carrier-pair", round_id, state,
        "capture round recorded from physical evidence", event["id"])
    return _apply(db, round_id, event["id"])


def record_image(db, event):
    """Append a late original image.  An issued sleeve is never rewritten."""
    setup(db)
    _reject_unknown(event, {"id", "kind", "round_id", "image"})
    round_id = event["round_id"]
    pair_round = one(db, "SELECT * FROM carrier_pair_round WHERE round_id=?", (round_id,))
    if not pair_round:
        raise CaptureError(f"unknown round {round_id}")
    if _count(db, "carrier_image") >= MAX_IMAGES:
        raise CaptureError(f"at most {MAX_IMAGES} label images may be recorded in total")
    capsule = _normalise_image(event["image"], pair_round["mode"])
    if one(db, "SELECT image_id FROM carrier_image WHERE image_id=?", (capsule["image_id"],)):
        raise CaptureError("image ids must be unique")
    if pair_round["mode"] == "paired_fixed_camera":
        _one_stop(_capsules(db, round_id) + [capsule])
    problem = _late_conflict(db, round_id, pair_round, capsule)
    if problem:
        log(db, "carrier-pair", round_id, "image_rejected", problem, event["id"])
        return _view(db, round_id, [{"action": "rejected",
                                     "code": capsule["readings"][0]["code"],
                                     "reason": problem,
                                     "images": [capsule["image_id"]]}])
    # A late frame is archived as evidence only.  It cannot bind a pending
    # position, because that would assemble a pair from separate screenshots.
    capsule["late"] = True
    _insert_image(db, round_id, capsule)
    log(db, "carrier-pair", round_id, "image_archived",
        "late image is archived evidence only; it cannot bind a position", event["id"])
    return _apply(db, round_id, event["id"])


def _late_conflict(db, round_id, pair_round, capsule):
    """A late frame may not contradict an issued sleeve or an already bound edge."""
    for reading in capsule["readings"]:
        if reading["page_side"] != "outer" or capsule["position"] is None:
            continue  # a reflection or an overview frame claims no edge
        locked = one(db, "SELECT * FROM carrier_code_position WHERE code=?", (reading["code"],))
        if locked and locked["position"] != capsule["position"]:
            return ("late image would move an already printed code to the other edge; "
                    "it is not admitted as evidence")
    for old in _capsules(db, round_id):
        if old["orientation"] != capsule["orientation"]:
            continue
        old_outer = {r["code"] for r in old["readings"] if r["page_side"] == "outer"}
        new_outer = {r["code"] for r in capsule["readings"] if r["page_side"] == "outer"}
        if old["position"] and old["position"] == capsule["position"] and old_outer and new_outer \
                and old_outer != new_outer:
            return "a second outer code on one physical position cannot join the same stop"
    return None


# --- split and envelope receipts --------------------------------------------

def start_split(db, event):
    setup(db)
    _reject_unknown(event, {"id", "kind", "round_id", "witness"})
    if not one(db, "SELECT round_id FROM carrier_pair_round WHERE round_id=?", (event["round_id"],)):
        raise CaptureError("split needs a recorded round on the shared turntable")
    if _count(db, "carrier_split_attempt") >= MAX_SPLITS:
        raise CaptureError("the site permits only one physically mutating split")
    if _count(db, "carrier_code_position"):
        raise CaptureError("a sleeve is already printed; splitting would make its source unverifiable")
    _clean(event.get("witness"), "split witness")
    db.execute("INSERT INTO carrier_split_attempt(round_id, state, detail) VALUES (?, 'open', ?)",
               (event["round_id"], "envelopes handed out; physical positions await receipts"))
    log(db, "carrier-pair", event["round_id"], "split_open",
        "carriers may move; only one envelope receipt can continue", event["id"])
    return "split_open"


def interrupt_split(db, event):
    setup(db)
    _reject_unknown(event, {"id", "kind", "round_id", "detail"})
    attempt = _latest_attempt(db)
    if not attempt or attempt["state"] != "open":
        raise CaptureError("no open split to interrupt")
    if _count(db, "carrier_split_attempt", "WHERE state='interrupted'") >= MAX_INTERRUPTS:
        raise CaptureError("the site permits only one interruption")
    if attempt["round_id"] != event.get("round_id"):
        raise CaptureError("interrupt names a different split round")
    detail = _clean(event.get("detail") or "physical state unknown after interruption",
                    "interrupt detail")
    db.execute("UPDATE carrier_split_attempt SET state='interrupted', detail=? WHERE attempt_id=?",
               (detail, attempt["attempt_id"]))
    log(db, "carrier-pair", attempt["round_id"], "split_interrupted",
        "cage position unknown; no unconditional second split or receipt", event["id"])
    return "split_interrupted"


def record_receipt(db, event):
    setup(db)
    _reject_unknown(event, {"id", "kind", "round_id", "position", "code",
                            "envelope_serial", "witness"})
    attempt = _latest_attempt(db)
    if not attempt:
        raise CaptureError("receipt without a split attempt")
    if attempt["state"] == "interrupted":
        raise CaptureError("split was interrupted; cage positions are unknown")
    if attempt["state"] == "closed":
        raise CaptureError("this split already closed its one physical position")
    position = event.get("position")
    if position not in LANES:
        raise CaptureError("receipt position must be left or right")
    if _count(db, "carrier_split_receipt", "WHERE attempt_id=?", (attempt["attempt_id"],)) >= MAX_RECEIPTS:
        raise CaptureError("the site permits only one locked envelope position")
    code = _clean(event.get("code"), "receipt code")
    serial = _clean(event.get("envelope_serial"), "envelope serial")
    witness = _clean(event.get("witness"), "receipt witness")
    db.execute(
        """INSERT INTO carrier_split_receipt(attempt_id, position, code, envelope_serial, witness)
           VALUES (?, ?, ?, ?, ?)""",
        (attempt["attempt_id"], position, code, serial, witness),
    )
    db.execute("UPDATE carrier_split_attempt SET state='closed', detail=? WHERE attempt_id=?",
               ("physical envelope locked one position; the other stays unproven",
                attempt["attempt_id"]))
    log(db, "carrier-pair", attempt["round_id"], "closed",
        f"envelope receipt locked {position}", event["id"])
    envelope_round = one(db, "SELECT round_id FROM carrier_pair_round "
                             "WHERE mode='separate_envelopes' ORDER BY rowid DESC LIMIT 1")
    if envelope_round:
        return _apply(db, envelope_round["round_id"], event["id"])
    return {"receipt": "closed", "position": position, "code": code,
            "next_action": {"action": "capture_sealed_envelope",
                            "reason": "the receipt fixes this position; photograph that one "
                                      "carrier inside its anti-reflective envelope"}}


# --- evidence planning (pure reads) -----------------------------------------

def _plan_paired(db, round_id, pair_round, images):
    if pair_round["state"] == "interrupted":
        return [{"action": "hold",
                 "reason": "shared turntable stop was not witnessed; no sleeve can be proven"}]
    # Late frames are archived but never positional: a pair must be supported by
    # the one original frame, not stitched from screenshots arriving later.
    images = [image for image in images if not image.get("late")]
    bound, ambiguous = {}, []
    for image in images:
        if image["orientation"] != pair_round["binding_orientation"]:
            continue
        codes = _bound_codes(image)
        if len(codes) > 1:
            ambiguous.append(image["image_id"])
            continue
        for code in codes:
            bound.setdefault(image["position"], set()).add(code)
    if ambiguous:
        return [{"action": "blocked",
                 "reason": f"image(s) {', '.join(ambiguous)} put more than one outer code on one "
                           "edge; label order cannot choose between them"}]
    for position, codes in bound.items():
        if len(codes) > 1:
            return [{"action": "blocked",
                     "reason": f"{position} shows several outer codes on the same stop"}]
    proven = {position: next(iter(codes)) for position, codes in bound.items()}
    if not proven:
        return [{"action": "hold",
                 "reason": "no outer page reading is bound to a physical side on this stop"}]
    prior = many(db, "SELECT * FROM carrier_code_position WHERE round_id<>? AND witness=?",
                 (round_id, "paired_fixed_camera"))
    for row in prior:
        if row["position"] not in proven:
            # A printed sleeve from another round cannot be completed here: that
            # is exactly the mirrored pair the desk must not assemble from stops.
            return [{"action": "blocked",
                     "reason": "a sleeve from another round cannot be completed here; a pair must "
                               "be supported by one witnessed turntable stop"}]
        if proven[row["position"]] != row["code"]:
            return [{"action": "blocked",
                     "reason": f"this stop reads {proven[row['position']]} on {row['position']} but "
                               f"{row['code']} was already printed there; contradictory stops "
                               "cannot be reconciled by a later image"}]
    if len(proven) == 2:
        if proven["left"] == proven["right"]:
            return [{"action": "blocked",
                     "reason": "one physical code cannot be printed to both sleeves"}]
        return [{"action": "issue", "position": lane, "code": proven[lane],
                 "images": _image_ids(images, lane)} for lane in LANES]
    position, code = next(iter(proven.items()))
    other = "right" if position == "left" else "left"
    others = [image for image in images if image["position"] in (None, other)]
    candidates = sorted({code for image in others for code in _outer_codes(image)} - {code})
    return [{"action": "issue", "position": position, "code": code,
             "images": _image_ids(images, position),
             "pending": {"position": other, "status": "unconfirmed",
                         "images": [image["image_id"] for image in others],
                         "candidate_codes": candidates,
                         "reason": "this edge has no outer reading on the same witnessed stop; a "
                                   "reflection, an overexposed frame, label order or a late "
                                   "screenshot cannot fill it"}}]


def _image_ids(images, position):
    return [image["image_id"] for image in images if image["position"] == position]


def _plan_split(db, images):
    attempt = _latest_attempt(db)
    if not attempt:
        return [{"action": "hold",
                 "reason": "separate envelopes exist but no physical receipt locked a position"}]
    images = [image for image in images if not image.get("late")]
    rows = many(db, "SELECT * FROM carrier_split_receipt WHERE attempt_id=?",
                (attempt["attempt_id"],))
    if not rows:
        reason = ("split interrupted before a receipt; cage positions are unknown"
                  if attempt["state"] == "interrupted"
                  else "split not yet closed by a physical receipt")
        return [{"action": "hold", "reason": reason}]
    receipt = rows[0]
    position, code, serial = receipt["position"], receipt["code"], receipt["envelope_serial"]
    # The receipt is the only source of a physical position.  An image may
    # corroborate it by envelope serial or outer code; image order assigns nothing.
    matching, contradicting = [], []
    for image in images:
        outer = _outer_codes(image)
        by_serial = image["envelope_serial"] == serial
        by_code = code in outer
        if by_serial and outer and outer != {code}:
            contradicting.append(image["image_id"])
        elif by_serial or by_code:
            matching.append(image)
    if contradicting:
        return [{"action": "blocked",
                 "reason": f"image(s) {', '.join(contradicting)} share the receipt's envelope "
                           f"serial but do not show {code}; a seal cannot be re-labelled"}]
    if not matching:
        return [{"action": "hold",
                 "reason": f"{position} receipt exists but no original image confirms its code"}]
    for image in matching:
        if image["position"] is not None and image["position"] != position:
            return [{"action": "blocked",
                     "reason": f"image {image['image_id']} claims {image['position']} but the "
                               f"receipt locks {code} at {position}"}]
    used = {image["image_id"] for image in matching}
    others = [image for image in images if image["image_id"] not in used]
    other = "right" if position == "left" else "left"
    return [{"action": "issue", "position": position, "code": code,
             "images": [image["image_id"] for image in matching],
             "pending": {"position": other, "status": "unconfirmed",
                         "images": [image["image_id"] for image in others],
                         "archived": [image["image_id"] for image in images if image.get("late")],
                         "candidate_codes": sorted({c for image in others for c in _outer_codes(image)}),
                         "reason": "the untouched position has no envelope receipt and no "
                                   "shared-stop witness; after a real split the paired source "
                                   "cannot be re-filmed"}}]


def _plan(db, round_id):
    pair_round = one(db, "SELECT * FROM carrier_pair_round WHERE round_id=?", (round_id,))
    images = _capsules(db, round_id)
    if pair_round["mode"] == "separate_envelopes":
        return _plan_split(db, images)
    return _plan_paired(db, round_id, pair_round, images)


def _issue(db, position, code, round_id, event_id, witness):
    """Lock one physical position to one code; a replay is idempotent."""
    by_code = one(db, "SELECT * FROM carrier_code_position WHERE code=?", (code,))
    if by_code:
        if by_code["position"] == position:
            return "issued", None
        return "blocked", f"code {code} was already locked to another physical position"
    if one(db, "SELECT code FROM carrier_code_position WHERE position=?", (position,)):
        return "blocked", f"position {position} already carries a different issued sleeve"
    db.execute("INSERT INTO carrier_code_position(code, position, witness, round_id) "
               "VALUES (?, ?, ?, ?)", (code, position, witness, round_id))
    db.execute(
        """INSERT INTO carrier_sleeve(slot, state, reading_id) VALUES (?, 'ready', ?)
           ON CONFLICT(slot) DO UPDATE SET state='ready', reading_id=excluded.reading_id""",
        (position, round_id),
    )
    log(db, "carrier-sleeve", position, "ready", "pair capture issued a real sleeve", event_id)
    return "issued", None


def _apply(db, round_id, event_id):
    witness = one(db, "SELECT mode FROM carrier_pair_round WHERE round_id=?", (round_id,))["mode"]
    outcomes = []
    for entry in _plan(db, round_id):
        if entry["action"] != "issue":
            outcomes.append(dict(entry))
            continue
        status, problem = _issue(db, entry["position"], entry["code"], round_id, event_id, witness)
        outcome = {"action": status, "position": entry["position"], "code": entry["code"],
                   "images": list(entry.get("images", []))}
        if problem:
            outcome["reason"] = problem
        if "pending" in entry:
            outcome["pending"] = entry["pending"]
        outcomes.append(outcome)
    return _view(db, round_id, outcomes)


def _describe(db, round_id):
    described = []
    for entry in _plan(db, round_id):
        if entry["action"] != "issue":
            described.append(dict(entry))
            continue
        locked = one(db, "SELECT code, position FROM carrier_code_position WHERE position=?",
                     (entry["position"],))
        item = {"action": "issued" if locked and locked["code"] == entry["code"] else "would_issue",
                "position": entry["position"], "code": entry["code"],
                "images": list(entry.get("images", []))}
        if "pending" in entry:
            item["pending"] = entry["pending"]
        described.append(item)
    return described


def _next_action(db, round_id, outcomes):
    """First step every still-compatible physical state agrees on."""
    mode = one(db, "SELECT mode FROM carrier_pair_round WHERE round_id=?", (round_id,))["mode"]
    issued = {row["position"] for row in many(
        db, "SELECT position FROM carrier_code_position WHERE round_id=?", (round_id,))}
    pending = [entry for entry in outcomes if "pending" in entry]
    if pending:
        side = pending[0]["pending"]["position"]
        if mode == "separate_envelopes":
            return {"action": "hold_unconfirmed_position", "position": side,
                    "reason": "only one envelope position can be locked; the other stays "
                              "unconfirmed and cannot be recovered from the split source"}
        return {"action": "re_witness_pending_side", "position": side,
                "reason": "one sleeve is printed; read the other fixed camera on one witnessed "
                          "turntable stop before any physical split"}
    blocked = any(entry["action"] == "blocked" for entry in outcomes)
    if blocked and issued:
        return {"action": "hold_remaining",
                "reason": "the issued sleeve stays locked; the remainder needs a new, "
                          "independently witnessed capture"}
    if blocked:
        return {"action": "do_not_split_yet",
                "reason": "re-witness both fixed cameras on one turntable stop first; a mirror of "
                          "two near-matches is not a shared frame"}
    if issued:
        return {"action": "done", "reason": "issued sleeves are locked; late images cannot rewrite them"}
    return {"action": "capture", "reason": "no sleeve is provable from the current evidence"}


def _view(db, round_id, outcomes=None):
    pair_round = one(db, "SELECT * FROM carrier_pair_round WHERE round_id=?", (round_id,))
    if not pair_round:
        raise CaptureError(f"unknown round {round_id}")
    described = outcomes if outcomes is not None else _describe(db, round_id)
    return {
        "round": {"round_id": pair_round["round_id"], "mode": pair_round["mode"],
                  "state": pair_round["state"], "interruption": pair_round["interruption"],
                  "witness": pair_round["witness"],
                  "binding_orientation": pair_round["binding_orientation"],
                  "binding_sides": pair_round["binding_sides"],
                  "images": _capsules(db, round_id)},
        "sleeves": [dict(row) for row in many(
            db, "SELECT code, position, round_id, witness FROM carrier_code_position "
                "WHERE round_id=? ORDER BY position", (round_id,))],
        "outcomes": described,
        "next_action": _next_action(db, round_id, described),
    }


def show(db, round_id=None):
    """Read-only view of a round's evidence and the next defensible action."""
    setup(db)
    if round_id is None:
        attempt = _latest_attempt(db)
        row = one(db, "SELECT round_id FROM carrier_pair_round ORDER BY rowid DESC LIMIT 1")
        round_id = (attempt["round_id"] if attempt else None) or (row["round_id"] if row else None)
    if not round_id:
        raise CaptureError("no capture round recorded")
    if not one(db, "SELECT round_id FROM carrier_pair_round WHERE round_id=?", (round_id,)):
        raise CaptureError(f"unknown round {round_id}")
    return _view(db, round_id)


def receipt(db):
    setup(db)
    return {
        "sleeves": [dict(row) for row in many(db, "SELECT * FROM carrier_code_position ORDER BY rowid")],
        "split_attempts": [dict(row) for row in many(db, "SELECT * FROM carrier_split_attempt ORDER BY attempt_id")],
        "receipts": [dict(row) for row in many(db, "SELECT * FROM carrier_split_receipt ORDER BY attempt_id, position")],
        "historical_limit": ("a code and a physical position are locked once issued; later images "
                             "cannot flip them, and a split ends the original paired-camera source"),
    }
