"""Communication desk follow-up and beacon source-evidence ledger.

The shared receiver light alone cannot prove which transmitter answered:
a beep may be a late answer from the old unit or an immediate answer from
the new one.  This module keeps the structured evidence (transmission
requests, slotted observations, interruptions, probe work orders and
readback proofs) that lets a duty collector tell the two sources apart,
and derives the follow-up todo from that evidence instead of the bare
receiver light.
"""

import json

from .store import log, many, one


def setup(db):
    db.execute(
        """CREATE TABLE IF NOT EXISTS comms_followup(
            shelter_id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            observation_id TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS beacon_bench(
            shelter_id TEXT PRIMARY KEY,
            ends TEXT NOT NULL,
            old_end TEXT NOT NULL,
            new_end TEXT NOT NULL,
            turnaround_lo INTEGER NOT NULL,
            turnaround_hi INTEGER NOT NULL,
            tones TEXT NOT NULL,
            keeps_pending_frames INTEGER NOT NULL,
            consumes_response_slot INTEGER NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS beacon_request(
            id TEXT PRIMARY KEY,
            shelter_id TEXT NOT NULL,
            slot INTEGER NOT NULL,
            state TEXT NOT NULL,
            answer_end TEXT,
            answer_slot INTEGER,
            answer_offset INTEGER,
            evidence_id TEXT
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS beacon_observation(
            id TEXT PRIMARY KEY,
            shelter_id TEXT NOT NULL,
            slot INTEGER NOT NULL,
            reader TEXT NOT NULL,
            status TEXT NOT NULL,
            tone INTEGER,
            consumed_request_id TEXT
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS beacon_interruption(
            id TEXT PRIMARY KEY,
            shelter_id TEXT NOT NULL,
            slot INTEGER NOT NULL,
            rewired TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS beacon_work_order(
            id TEXT PRIMARY KEY,
            shelter_id TEXT NOT NULL,
            tool TEXT NOT NULL,
            plan TEXT NOT NULL,
            status TEXT NOT NULL,
            result TEXT
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS beacon_proof(
            id INTEGER PRIMARY KEY,
            shelter_id TEXT NOT NULL,
            end TEXT NOT NULL,
            kind TEXT NOT NULL,
            evidence_id TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS beacon_consumed_slot(
            shelter_id TEXT NOT NULL,
            slot INTEGER NOT NULL,
            evidence_id TEXT NOT NULL,
            PRIMARY KEY(shelter_id, slot)
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


# --------------------------------------------------------------------------
# structured evidence path (a bench of two simulated ends is declared)
# --------------------------------------------------------------------------

def _slot(event):
    slot = event["slot"]
    if isinstance(slot, bool) or not isinstance(slot, int) or slot < 0:
        raise ValueError("slot must be a non-negative integer time slot")
    return slot


def _bench(db, shelter_id):
    row = one(db, "SELECT * FROM beacon_bench WHERE shelter_id=?", (shelter_id,))
    if row is None:
        return None
    return {
        "ends": {k: tuple(v) for k, v in json.loads(row["ends"]).items()},
        "old": row["old_end"],
        "new": row["new_end"],
        "lo": row["turnaround_lo"],
        "hi": row["turnaround_hi"],
        "tones": json.loads(row["tones"]),
        "keeps_pending_frames": bool(row["keeps_pending_frames"]),
        "consumes_response_slot": bool(row["consumes_response_slot"]),
    }


def has_bench(db, shelter_id):
    setup(db)
    return _bench(db, shelter_id) is not None


def _enabled(bench, end, slot):
    first, last = bench["ends"][end]
    return first <= slot <= last


def _chances(bench, req):
    """Every (end, offset, answer_slot) on which a request could be answered."""
    out = []
    for end in bench["ends"]:
        if not _enabled(bench, end, req["slot"]):
            continue
        for offset in range(bench["lo"], bench["hi"] + 1):
            slot = req["slot"] + offset
            if _enabled(bench, end, slot):
                out.append((end, offset, slot))
    return out


def _shared_by_slot(db, shelter_id):
    rows = many(
        db,
        "SELECT * FROM beacon_observation WHERE shelter_id=? AND reader='shared' ORDER BY slot",
        (shelter_id,),
    )
    return {r["slot"]: r for r in rows}


def _interrupted_slots(db, shelter_id):
    return {
        r["slot"]
        for r in many(db, "SELECT slot FROM beacon_interruption WHERE shelter_id=?", (shelter_id,))
    }


def _consumed_slots(db, shelter_id):
    return {
        r["slot"]
        for r in many(db, "SELECT slot FROM beacon_consumed_slot WHERE shelter_id=?", (shelter_id,))
    }


def _alive_chances(bench, req, shared, interrupted, consumed_slots):
    """Chances of an outstanding request that may still yield an original.

    A chance is dead once the shared reader logged the slot (the beep was
    spent on another request, or the slot was silent), once an interruption
    left the slot unreceivable, or once a loopback wiring consumed the slot.
    Returns (end, offset, slot, shared_observation_id_or_None) tuples.
    """
    alive = []
    for end, offset, slot in _chances(bench, req):
        obs = shared.get(slot)
        if obs is not None:
            if obs["status"] == "missing":
                continue
            if obs["consumed_request_id"] is not None:
                continue
        if slot in interrupted or slot in consumed_slots:
            continue
        alive.append((end, offset, slot, obs["id"] if obs is not None else None))
    return alive


def _prove(db, shelter_id, end, kind, evidence_id):
    if one(
        db,
        "SELECT 1 FROM beacon_proof WHERE shelter_id=? AND end=? AND kind=? AND evidence_id=?",
        (shelter_id, end, kind, evidence_id),
    ):
        return
    db.execute(
        "INSERT INTO beacon_proof(shelter_id,end,kind,evidence_id) VALUES (?,?,?,?)",
        (shelter_id, end, kind, evidence_id),
    )
    log(db, "comms", shelter_id, "end_proven", "%s proven online by %s" % (end, kind), evidence_id)


def _answer(db, req_id, end, slot, offset, evidence_id):
    db.execute(
        """UPDATE beacon_request
           SET state='answered', answer_end=?, answer_slot=?, answer_offset=?, evidence_id=?
           WHERE id=?""",
        (end, slot, offset, evidence_id, req_id),
    )


def _outstanding(db, shelter_id):
    return many(
        db,
        "SELECT * FROM beacon_request WHERE shelter_id=? AND state='outstanding' ORDER BY id",
        (shelter_id,),
    )


def _attribute(db, shelter_id, bench):
    """Consume shared beeps that only one outstanding request can explain.

    One beep consumes exactly one transmission number: a shared 'heard'
    stays unattributed while more than one request could explain it, and a
    request is never answered twice.  When every surviving explanation of a
    beep names the same end, that end is proven even if the exact request
    stays ambiguous.
    """
    while True:
        shared = _shared_by_slot(db, shelter_id)
        interrupted = _interrupted_slots(db, shelter_id)
        consumed_slots = _consumed_slots(db, shelter_id)
        outstanding = _outstanding(db, shelter_id)
        consumed_one = False
        for slot in sorted(shared):
            obs = shared[slot]
            if obs["status"] != "heard" or obs["consumed_request_id"] is not None:
                continue
            explanations = []
            for req in outstanding:
                for end, offset, answer_slot, obs_id in _alive_chances(
                    bench, req, shared, interrupted, consumed_slots
                ):
                    if obs_id == obs["id"]:
                        explanations.append((req["id"], end, offset))
            if not explanations:
                continue
            ends = {end for _, end, _ in explanations}
            if len(ends) == 1:
                _prove(db, shelter_id, ends.pop(), "shared_unique", obs["id"])
            if len(explanations) == 1:
                req_id, end, offset = explanations[0]
                _answer(db, req_id, end, obs["slot"], offset, obs["id"])
                db.execute(
                    "UPDATE beacon_observation SET consumed_request_id=? WHERE id=?",
                    (req_id, obs["id"]),
                )
                log(
                    db, "comms", shelter_id, "beacon_answered",
                    "shared beep has a single surviving explanation", obs["id"],
                )
                consumed_one = True
                break
        if not consumed_one:
            break
    shared = _shared_by_slot(db, shelter_id)
    interrupted = _interrupted_slots(db, shelter_id)
    consumed_slots = _consumed_slots(db, shelter_id)
    for req in _outstanding(db, shelter_id):
        if not _alive_chances(bench, req, shared, interrupted, consumed_slots):
            db.execute("UPDATE beacon_request SET state='expired' WHERE id=?", (req["id"],))
            log(
                db, "comms", shelter_id, "beacon_expired",
                "every answer slot for this request has passed silently", req["id"],
            )


def refresh(db, shelter_id, reason, event_id):
    """Recompute attribution and derive the follow-up todo from all evidence."""
    setup(db)
    bench = _bench(db, shelter_id)
    if bench is None:
        raise ValueError("a declared bench is required for structured beacon evidence")
    _attribute(db, shelter_id, bench)
    proven_new = one(
        db, "SELECT 1 FROM beacon_proof WHERE shelter_id=? AND end=?", (shelter_id, bench["new"])
    )
    state = "monitor" if proven_new else "request_readback"
    db.execute(
        """INSERT INTO comms_followup VALUES (?, ?, ?)
           ON CONFLICT(shelter_id) DO UPDATE SET
              state=excluded.state, observation_id=excluded.observation_id""",
        (shelter_id, state, event_id),
    )
    log(db, "comms", shelter_id, state, reason, event_id)
    return state


def declare_bench(db, shelter_id, event):
    setup(db)
    ends = event["ends"]
    if len(ends) != 2:
        raise ValueError("the bench models exactly two simulated ends")
    for name, window in ends.items():
        if len(window) != 2 or not all(isinstance(s, int) for s in window) or window[0] > window[1]:
            raise ValueError("each end needs a [first, last] enabled window of integer slots")
    lo, hi = event["turnaround"]
    if not (isinstance(lo, int) and isinstance(hi, int) and 0 <= lo <= hi):
        raise ValueError("invalid turnaround range")
    # the later-starting end is the replacement unit the desk must prove
    ordered = sorted(ends.items(), key=lambda kv: (kv[1][0], kv[1][1], kv[0]))
    (old_end, _), (new_end, _) = ordered
    db.execute(
        "INSERT INTO beacon_bench VALUES (?,?,?,?,?,?,?,?,?)",
        (
            shelter_id,
            json.dumps(ends, sort_keys=True),
            old_end,
            new_end,
            lo,
            hi,
            json.dumps(list(event["tones"])),
            int(bool(event.get("cold_reader_keeps_pending_frames"))),
            int(bool(event.get("loopback_control_consumes_response_slot"))),
        ),
    )
    log(
        db, "comms", shelter_id, "bench_declared",
        "two simulated ends share the receiver; the light alone cannot source a beep",
        event["id"],
    )
    return refresh(db, shelter_id, "bench declared; replacement transmitter unproven", event["id"])


def add_request(db, shelter_id, event):
    setup(db)
    bench = _bench(db, shelter_id)
    if bench is None:
        raise ValueError("a declared bench is required before issuing requests")
    slot = _slot(event)
    if not any(_enabled(bench, end, slot) for end in bench["ends"]):
        raise ValueError("no simulated end is enabled to receive this request")
    db.execute(
        "INSERT INTO beacon_request(id,shelter_id,slot,state) VALUES (?,?,?,'outstanding')",
        (event["id"], shelter_id, slot),
    )
    log(db, "beacon", shelter_id, "request", "transmission number issued", event["id"])
    return refresh(db, shelter_id, "evidence recompute after request", event["id"])


def record_observation(db, shelter_id, event):
    setup(db)
    bench = _bench(db, shelter_id)
    if bench is None:
        raise ValueError("a slotted heartbeat requires a declared bench")
    slot = _slot(event)
    status = event["observed"]
    tone = event.get("tone")
    if status == "heard":
        if tone not in bench["tones"]:
            raise ValueError("heard tone must be one of the bench tones")
    elif tone is not None:
        raise ValueError("a missing reading carries no tone")
    if event.get("reader", "shared") != "shared":
        raise ValueError("only the shared receiver logs heartbeats; cold and loopback arrive via readback")
    if one(
        db,
        "SELECT 1 FROM beacon_observation WHERE shelter_id=? AND reader='shared' AND slot=?",
        (shelter_id, slot),
    ):
        raise ValueError("the shared receiver already logged this slot")
    db.execute(
        "INSERT INTO beacon_observation(id,shelter_id,slot,reader,status,tone) VALUES (?,?,?,?,?,?)",
        (event["id"], shelter_id, slot, "shared", status, tone),
    )
    log(db, "beacon", shelter_id, status, "slotted shared receiver reading", event["id"])
    return refresh(db, shelter_id, "evidence recompute after slotted heartbeat", event["id"])


def add_interruption(db, shelter_id, event):
    setup(db)
    if _bench(db, shelter_id) is None:
        raise ValueError("a declared bench is required before logging an interruption")
    slot = _slot(event)
    rewired = event.get("rewired", "unknown")
    if rewired not in {"unknown", "confirmed_clear"}:
        raise ValueError("rewired must be unknown or confirmed_clear")
    db.execute(
        "INSERT INTO beacon_interruption VALUES (?,?,?,?)",
        (event["id"], shelter_id, slot, rewired),
    )
    log(
        db, "beacon", shelter_id, "interruption",
        "desk action claimed but powered off; return slot not defaulted to unoccupied",
        event["id"],
    )
    return refresh(db, shelter_id, "evidence recompute after interruption", event["id"])


def plan_probe(db, shelter_id, event):
    """Issue the one capture plan the remaining duty window can fulfill.

    Exactly one field tool may be attached in the window.  The active
    loopback proves the replacement end on the spot but its wiring consumes
    the response slot; the cold reader only watches preserved frames and
    consumes nothing, yet can stay inconclusive.  The plan records the ends
    the existing evidence supports, what is still observable, whether the
    step ends all switching ambiguity, and the real gaps when it does not.
    """
    setup(db)
    bench = _bench(db, shelter_id)
    if bench is None:
        raise ValueError("a declared bench is required before planning a probe")
    constraint = event.get("constraint")
    if constraint not in {"no_overwrite", "confirmed_active"}:
        raise ValueError("constraint must be no_overwrite or confirmed_active")
    window = sorted({int(s) for s in event["window"]})
    if not window:
        raise ValueError("the remaining duty window must name at least one slot")
    _attribute(db, shelter_id, bench)
    shared = _shared_by_slot(db, shelter_id)
    interrupted = {
        r["slot"]: r["rewired"]
        for r in many(
            db, "SELECT slot, rewired FROM beacon_interruption WHERE shelter_id=? ORDER BY slot",
            (shelter_id,),
        )
    }
    consumed_slots = _consumed_slots(db, shelter_id)
    outstanding = _outstanding(db, shelter_id)
    pending = {}
    for req in outstanding:
        for end, offset, slot, obs_id in _alive_chances(bench, req, shared, interrupted, consumed_slots):
            if obs_id is None:
                pending.setdefault(slot, []).append(req["id"])
    supported = [
        r["end"]
        for r in many(
            db, "SELECT DISTINCT end FROM beacon_proof WHERE shelter_id=? ORDER BY end",
            (shelter_id,),
        )
    ]
    observables = {"pending_original_slots": sorted(pending), "shared_log_slots": sorted(shared)}
    blocked = {}
    choices = []
    for slot in window:
        if not _enabled(bench, bench["new"], slot):
            blocked[slot] = "replacement end is not enabled on this slot"
            continue
        if interrupted.get(slot) == "unknown":
            blocked[slot] = "desk action claimed but powered off; slot not defaulted to unoccupied"
            continue
        if slot in consumed_slots:
            blocked[slot] = "response slot already consumed by an earlier wiring"
            continue
        obs = shared.get(slot)
        if obs is not None and obs["status"] == "heard":
            blocked[slot] = "a physical response already arrived on this slot"
            continue
        losses = sorted(pending.get(slot, []))
        if losses and constraint == "no_overwrite":
            blocked[slot] = "hard rule: would overwrite uncollected originals of " + ",".join(losses)
            continue
        choices.append((slot, losses))
    if choices:
        slot, losses = choices[0]
        plan = {
            "tool": "loopback",
            "slot": slot,
            "label": event["id"] + "-label",
            "consumes": [{"request": r, "slot": slot} for r in losses],
            "supported_ends": supported,
            "observables": dict(observables, wired_slot=slot, response_tones=bench["tones"]),
            "minimal_observations": ["tone of the wired response at slot %d" % slot],
            "resolves_ambiguity": True,
            "gaps": (
                ["wiring slot %d spends the pending originals of %s" % (slot, ",".join(losses))]
                if losses else []
            ),
            "blocked_loopback_slots": {str(s): why for s, why in blocked.items()},
        }
    else:
        if not bench["keeps_pending_frames"]:
            raise ValueError("no legal capture: loopback blocked and the cold reader keeps no frames")
        ambiguous = sorted(
            s for s, o in shared.items()
            if o["status"] == "heard" and o["consumed_request_id"] is None
        )
        points = [ambiguous[0], ambiguous[0] + 1] if ambiguous else []
        new = bench["new"]
        gaps = []
        requested = many(db, "SELECT slot FROM beacon_request WHERE shelter_id=?", (shelter_id,))
        if not any(_enabled(bench, new, r["slot"]) for r in requested):
            gaps.append("new end %s never had a real request; nothing to read back" % new)
        unseen = sorted({
            slot
            for req in outstanding
            for (end, offset, slot, obs_id) in _alive_chances(
                bench, req, shared, interrupted, consumed_slots
            )
            if end == new and slot not in points
        })
        if unseen:
            gaps.append(
                "new end %s answer slots %s fall outside the sampled points %s"
                % (new, unseen, points)
            )
        unknown = [s for s in points if interrupted.get(s) == "unknown"]
        if unknown:
            gaps.append("sampled rounds %s have unknown history after the interruption" % unknown)
        plan = {
            "tool": "cold_reader",
            "points": points,
            "supported_ends": supported,
            "observables": dict(observables, sample_points=points),
            "minimal_observations": ["branch of the preserved frame at slot %d" % s for s in points],
            "resolves_ambiguity": not gaps,
            "gaps": gaps,
            "blocked_loopback_slots": {str(s): why for s, why in blocked.items()},
        }
    db.execute(
        "INSERT INTO beacon_work_order(id,shelter_id,tool,plan,status) VALUES (?,?,?,?,'planned')",
        (event["id"], shelter_id, plan["tool"], json.dumps(plan, sort_keys=True)),
    )
    log(
        db, "comms", shelter_id, "probe_planned",
        "capture plan issued for the remaining duty window", event["id"],
    )
    return plan


def _open_gaps(db, shelter_id, bench):
    """Real gaps left after the latest evidence: unproven ends and spent beeps."""
    gaps = []
    if not one(
        db, "SELECT 1 FROM beacon_proof WHERE shelter_id=? AND end=?", (shelter_id, bench["new"])
    ):
        gaps.append("new end %s is not proven online" % bench["new"])
    shared = _shared_by_slot(db, shelter_id)
    interrupted = _interrupted_slots(db, shelter_id)
    consumed_slots = _consumed_slots(db, shelter_id)
    outstanding = _outstanding(db, shelter_id)
    for slot in sorted(shared):
        obs = shared[slot]
        if obs["status"] != "heard" or obs["consumed_request_id"] is not None:
            continue
        reqs = sorted({
            req["id"]
            for req in outstanding
            for chance in _alive_chances(bench, req, shared, interrupted, consumed_slots)
            if chance[3] == obs["id"]
        })
        if len(reqs) > 1:
            gaps.append(
                "shared beep %s at slot %d fits %s; one beep consumes one transmission number"
                % (obs["id"], obs["slot"], ",".join(reqs))
            )
        elif not reqs:
            gaps.append(
                "shared beep %s at slot %d has no surviving explanation" % (obs["id"], obs["slot"])
            )
    return gaps


def _apply_cold(db, shelter_id, bench, plan, event):
    points = sorted(plan["points"])
    if sorted(event["points"]) != points:
        raise ValueError("the cold reader must be attached to the planned sampling points")
    frames = event.get("frames")
    if not isinstance(frames, list):
        raise ValueError("a cold readback must list the preserved frames it recovered")
    prepared = []
    seen = set()
    for frame in frames:
        req = one(
            db,
            "SELECT * FROM beacon_request WHERE id=? AND shelter_id=?",
            (frame["request_id"], shelter_id),
        )
        if req is None:
            raise ValueError("frame answers an unknown request")
        if req["state"] != "outstanding":
            raise ValueError("request original already consumed; it cannot be spent again")
        if req["id"] in seen:
            raise ValueError("one readback cannot spend the same transmission number twice")
        seen.add(req["id"])
        end = frame["end"]
        if end not in bench["ends"]:
            raise ValueError("frame names an unknown end")
        offset = frame["slot"] - req["slot"]
        if not bench["lo"] <= offset <= bench["hi"]:
            raise ValueError("answer offset outside the turnaround range")
        if not (_enabled(bench, end, req["slot"]) and _enabled(bench, end, frame["slot"])):
            raise ValueError("end was not enabled for this frame")
        prepared.append((req, end, frame["slot"], offset, frame.get("tone")))
    offsets = {}
    for row in many(
        db,
        "SELECT answer_end, answer_offset FROM beacon_request WHERE shelter_id=? AND state='answered'",
        (shelter_id,),
    ):
        offsets.setdefault(row["answer_end"], set()).add(row["answer_offset"])
    for req, end, slot, offset, tone in prepared:
        offsets.setdefault(end, set()).add(offset)
    for end, values in offsets.items():
        if len(values) > 1:
            raise ValueError("same-end common offset violated for " + end)
    consumed_slots = _consumed_slots(db, shelter_id)
    for req, end, slot, offset, tone in prepared:
        if slot not in points:
            raise ValueError("frame falls outside the sampled points")
        if slot in consumed_slots:
            raise ValueError("response slot already consumed by loopback wiring")
        obs = one(
            db,
            "SELECT * FROM beacon_observation WHERE shelter_id=? AND reader='shared' AND slot=?",
            (shelter_id, slot),
        )
        if obs is not None:
            if obs["status"] == "missing":
                raise ValueError("frame contradicts the shared receiver log")
            if obs["consumed_request_id"] not in (None, req["id"]):
                raise ValueError("shared beep already spent on another transmission number")
        if tone is not None and tone not in bench["tones"]:
            raise ValueError("frame carries an unknown tone")
    answered = []
    for req, end, slot, offset, tone in prepared:
        _answer(db, req["id"], end, slot, offset, event["id"])
        obs = one(
            db,
            "SELECT * FROM beacon_observation WHERE shelter_id=? AND reader='shared' AND slot=?",
            (shelter_id, slot),
        )
        if obs is not None and obs["consumed_request_id"] is None:
            db.execute(
                "UPDATE beacon_observation SET consumed_request_id=? WHERE id=?",
                (req["id"], obs["id"]),
            )
        db.execute(
            "INSERT INTO beacon_observation(id,shelter_id,slot,reader,status,tone) VALUES (?,?,?,?,?,?)",
            (event["id"] + "-" + req["id"], shelter_id, slot, "cold", "heard", tone),
        )
        _prove(db, shelter_id, end, "cold_frame", event["id"])
        answered.append(req["id"])
        log(
            db, "comms", shelter_id, "beacon_answered",
            "cold reader recovered the original frame", event["id"],
        )
    return {"answered": sorted(answered), "consumed": [], "proven": sorted({e for _, e, _, _, _ in prepared})}


def _apply_loopback(db, shelter_id, bench, plan, event):
    if event["slot"] != plan["slot"]:
        raise ValueError("the loopback must be wired to the planned slot")
    if event.get("label") != plan["label"]:
        raise ValueError("the sealed label is sent exactly once, as planned")
    slot = plan["slot"]
    responses = event.get("responses")
    if not isinstance(responses, list):
        raise ValueError("a loopback readback must list the wired responses")
    tones = {}
    for response in responses:
        end = response["end"]
        if end not in bench["ends"]:
            raise ValueError("response names an unknown end")
        if response["tone"] not in bench["tones"]:
            raise ValueError("response carries an unknown tone")
        if end in tones:
            raise ValueError("duplicate response for one end")
        tones[end] = response["tone"]
    if len(set(tones.values())) != len(tones):
        raise ValueError("the wired test must make the two ends answer with different outputs")
    shared = _shared_by_slot(db, shelter_id)
    interrupted = _interrupted_slots(db, shelter_id)
    consumed_slots = _consumed_slots(db, shelter_id)
    consumed = []
    for req in _outstanding(db, shelter_id):
        chances = _alive_chances(bench, req, shared, interrupted, consumed_slots)
        if not any(answer_slot == slot for _, _, answer_slot, _ in chances):
            continue
        remaining = [c for c in chances if c[2] != slot]
        if not remaining:
            db.execute(
                "UPDATE beacon_request SET state='consumed', evidence_id=? WHERE id=?",
                (event["id"], req["id"]),
            )
            consumed.append(req["id"])
            log(
                db, "comms", shelter_id, "beacon_consumed",
                "loopback wiring overwrote the last uncollected original", event["id"],
            )
    db.execute(
        "INSERT INTO beacon_consumed_slot VALUES (?,?,?)", (shelter_id, slot, event["id"])
    )
    proven = []
    for end in sorted(tones):
        db.execute(
            "INSERT INTO beacon_observation(id,shelter_id,slot,reader,status,tone) VALUES (?,?,?,?,?,?)",
            (event["id"] + "-" + end, shelter_id, slot, "loopback", "heard", tones[end]),
        )
        _prove(db, shelter_id, end, "loopback_response", event["id"])
        proven.append(end)
    return {"answered": [], "consumed": sorted(consumed), "proven": proven}


def apply_readback(db, shelter_id, event):
    """Apply the outcome of the planned capture to the evidence ledger."""
    setup(db)
    bench = _bench(db, shelter_id)
    if bench is None:
        raise ValueError("a declared bench is required before readback")
    wo = one(
        db,
        """SELECT * FROM beacon_work_order
           WHERE shelter_id=? AND status IN ('planned','pending')
           ORDER BY rowid DESC LIMIT 1""",
        (shelter_id,),
    )
    if wo is None:
        raise ValueError("no open work order; plan a probe before reading back")
    plan = json.loads(wo["plan"])
    tool = event.get("tool")
    if tool != plan["tool"]:
        raise ValueError("readback does not match the planned capture")
    if tool == "cold_reader":
        result = _apply_cold(db, shelter_id, bench, plan, event)
    elif tool == "loopback":
        result = _apply_loopback(db, shelter_id, bench, plan, event)
    else:
        raise ValueError("unknown capture tool")
    state = refresh(db, shelter_id, "evidence recompute after readback", event["id"])
    result["gaps"] = _open_gaps(db, shelter_id, bench)
    status = (
        "closed"
        if one(db, "SELECT 1 FROM beacon_proof WHERE shelter_id=? AND end=?", (shelter_id, bench["new"]))
        else "pending"
    )
    db.execute(
        "UPDATE beacon_work_order SET status=?, result=? WHERE id=?",
        (status, json.dumps(result, sort_keys=True), wo["id"]),
    )
    log(db, "comms", shelter_id, "work_order_" + status, "readback evidence applied", event["id"])
    return {"followup": state, "work_order": status, **result}
