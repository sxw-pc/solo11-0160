"""Intake for synthetic volunteer team service claims and field evidence.

The two desks file short-interval work sheets that may observe the same real
work event. Sheets are retained verbatim as a report summary; certification of
person-minutes happens in :mod:`shelterledger.crew_credit` from independently
held originals: in-shift badge anchors with two-desk co-witnessed forwarding
(route A), or double-signed simultaneous occupancy at mutually exclusive tool
stations (route B). Local desk codes are anonymous shift credentials and are
never counted as two people on their own.
"""

import json

from .crew_credit import reconcile, DEFAULT_SHIFT, BADGE, STATION, BUTTON, BACKUP
from .store import log, one


def _shift(event):
    return str(event.get("shift", DEFAULT_SHIFT))


def _round(event):
    return int(event.get("round", 1))


def _payload(event, keys):
    missing = [key for key in keys if key not in event]
    if missing:
        raise ValueError(f"evidence event missing {', '.join(missing)}")
    return {key: event[key] for key in keys}


def _insert_evidence(db, event, kind, state, reason, signed_at, start, stop, payload):
    db.execute(
        "INSERT INTO crew_evidence(id,team,kind,shift,round,state,reason,signed_at,covers_start,covers_stop,payload,event_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            event["id"],
            event["team"],
            kind,
            _shift(event),
            _round(event),
            state,
            reason,
            signed_at,
            start,
            stop,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            event["id"],
        ),
    )


def _late_quota(db, team):
    if one(db, "SELECT COUNT(*) AS n FROM crew_late_action WHERE team=?", (team,))["n"]:
        raise ValueError("only one read-back or re-sign witness action is allowed for the next shift")


def _window(db, event):
    minutes = int(event["sheet_minutes"])
    start, stop = int(event["start"]), int(event["stop"])
    if minutes < 0 or not event.get("witness") or start < 0 or stop <= start:
        raise ValueError("nonnegative witnessed work minutes covering a positive interval are required")
    if minutes != stop - start:
        raise ValueError("sheet minutes must match the covered [start, stop) interval")
    db.execute(
        "INSERT INTO crew_window(id,team,minutes,claim,witness,start,stop,desk,local_code,shift,round) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            event["id"],
            event["team"],
            minutes,
            int(event["claim_minutes"]),
            event["witness"],
            start,
            stop,
            event.get("desk"),
            event.get("local_code"),
            _shift(event),
            _round(event),
        ),
    )
    return reconcile(db, event["team"], int(event["claim_minutes"]), event["id"], _shift(event))


def _badge_anchor(db, event):
    payload = _payload(event, ["badge_id", "issued_at", "links"])
    if not event.get("witness") or int(event["issued_at"]) < 0:
        raise ValueError("badge issuance anchor needs a witness and issuance time")
    for link in payload["links"]:
        if "desk" not in link or "local_code" not in link:
            raise ValueError("each forwarded credential needs desk and local_code")
        for other in link.get("same_as", []):
            if len(set(other.get("witnesses", []))) < 2:
                raise ValueError("cross-desk forwarding needs two-desk joint witnesses")
    state, reason = "recorded", "in-shift issuance anchor with co-witnessed credential forwarding"
    _insert_evidence(db, event, BADGE, state, reason, int(payload["issued_at"]), None, None, payload)
    return reconcile(db, event["team"], int(event["claim_minutes"]), event["id"], _shift(event))


def _station_witness(db, event):
    payload = _payload(event, ["station", "signed_at", "covers_start", "covers_stop", "signatures", "simultaneous_occupancy"])
    start, stop = int(payload["covers_start"]), int(payload["covers_stop"])
    signed_at = int(payload["signed_at"])
    independent = len(set(payload["signatures"])) >= 2
    in_period = start <= signed_at < stop
    simultaneous = bool(payload["simultaneous_occupancy"])
    if independent and in_period and simultaneous:
        state, reason = "recorded", "independent double-signed simultaneous occupancy at an exclusive station"
    else:
        state, reason = (
            "insufficient",
            "cannot establish two simultaneous contributors without two independent in-period signatures over simultaneous occupancy",
        )
    _insert_evidence(db, event, STATION, state, reason, signed_at, start, stop, payload)
    return reconcile(db, event["team"], int(event["claim_minutes"]), event["id"], _shift(event))


def _button_receipt(db, event):
    payload = _payload(event, ["station", "signed_at", "covers_start", "covers_stop"])
    _insert_evidence(
        db,
        event,
        BUTTON,
        "insufficient",
        "button work receipt cannot stand in for a second simultaneously occupied contributor",
        int(payload["signed_at"]),
        int(payload["covers_start"]),
        int(payload["covers_stop"]),
        payload,
    )
    log(db, "crew", event["team"], "insufficient", "station movement receipt retained but not credited", event["id"])
    return reconcile(db, event["team"], int(event["claim_minutes"]), event["id"], _shift(event))


def _backup_retrieval(db, event):
    _late_quota(db, event["team"])
    payload = _payload(event, ["station", "signed_at", "covers_start", "covers_stop", "signatures", "simultaneous_occupancy"])
    start, stop = int(payload["covers_start"]), int(payload["covers_stop"])
    signed_at = int(payload["signed_at"])
    independent = len(set(payload["signatures"])) >= 2
    in_period = start <= signed_at < stop
    if not (independent and in_period and bool(payload["simultaneous_occupancy"])):
        state, reason = (
            "rejected",
            "a retrieved backup must be an original independently signed inside the work period; it cannot be newly made",
        )
    else:
        state, reason = "recorded", "retrieved previously independent-signed backup of simultaneous station occupancy"
    _insert_evidence(db, event, BACKUP, state, reason, signed_at, start, stop, payload)
    if state == "recorded":
        db.execute(
            "INSERT INTO crew_late_action(team,shift,action,evidence_id,event_id) VALUES(?,?,?,?,?)",
            (event["team"], _shift(event), "backup_retrieval", event["id"], event["id"]),
        )
    log(db, "crew", event["team"], state, reason, event["id"])
    return reconcile(db, event["team"], int(event["claim_minutes"]), event["id"], _shift(event))


def _witness_resign(db, event):
    _late_quota(db, event["team"])
    payload = _payload(event, ["target_evidence_id", "badge_id", "links"])
    target = one(db, "SELECT * FROM crew_evidence WHERE id=? AND team=?", (payload["target_evidence_id"], event["team"]))
    if not target:
        raise ValueError("re-sign must name an existing field original")
    if target["kind"] in (STATION, BUTTON):
        state, reason = (
            "rejected",
            "simultaneous occupancy cannot be produced after the period; only an already-signed backup may be retrieved",
        )
        db.execute("UPDATE crew_evidence SET state=?, reason=? WHERE id=?", (state, reason, target["id"]))
        log(db, "crew", event["team"], state, reason, event["id"])
        return reconcile(db, event["team"], int(event["claim_minutes"]), event["id"], _shift(event))
    merged = json.loads(target["payload"])
    for link in payload["links"]:
        if "desk" not in link or "local_code" not in link:
            raise ValueError("each forwarded credential needs desk and local_code")
        for other in link.get("same_as", []):
            if len(set(other.get("witnesses", []))) < 2:
                raise ValueError("cross-desk forwarding needs two-desk joint witnesses")
    merged.setdefault("links", []).extend(payload["links"])
    merged["resigned_badge_id"] = payload["badge_id"]
    db.execute(
        "UPDATE crew_evidence SET payload=?, state='recorded', reason=? WHERE id=?",
        (
            json.dumps(merged, ensure_ascii=False, sort_keys=True),
            "identity back-filled from a still-existing real badge issuance root",
            target["id"],
        ),
    )
    db.execute(
        "INSERT INTO crew_late_action(team,shift,action,evidence_id,event_id) VALUES(?,?,?,?,?)",
        (event["team"], _shift(event), "witness_resign", target["id"], event["id"]),
    )
    log(db, "crew", event["team"], "recorded", "one allowed next-shift re-sign restored badge identity", event["id"])
    return reconcile(db, event["team"], int(event["claim_minutes"]), event["id"], _shift(event))


_KINDS = {
    "crew_window": _window,
    "badge_anchor": _badge_anchor,
    "station_witness": _station_witness,
    "button_receipt": _button_receipt,
    "backup_retrieval": _backup_retrieval,
    "witness_resign": _witness_resign,
}


def capture(db, event):
    handler = _KINDS.get(event["kind"])
    if handler is None:
        raise ValueError(f"unknown crew evidence kind {event['kind']!r}")
    return handler(db, event)
