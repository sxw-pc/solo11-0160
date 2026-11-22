"""Probe-planning tests: telling a late old answer from a live new one.

The bench fixture defines two simulated ends overlapping on integer slots.
The shared receiver beep at the same_prefix slot can be the old unit's
late answer or the new unit's immediate answer, so the desk must plan one
legal supplementary capture inside the remaining duty window and record
enough for the follow-up work order to close or stay pending.
"""

import json
from pathlib import Path

import pytest

from shelterledger.__main__ import HANDLERS, import_file
from shelterledger.report import audit, report
from shelterledger.store import connect

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
BENCH = json.loads((FIXTURES / "beacon_probe_bench.json").read_text(encoding="utf-8"))
DESK = BENCH["desk"]
OLD_END, NEW_END = "tx_a", "tx_b"
SAME_PREFIX_SLOT = BENCH["same_prefix"]["slot"]
SAME_PREFIX_TONE = BENCH["same_prefix"]["tone"]
NEW_TONE = BENCH["beacon_tones"][1]


def bench_event():
    return {
        "id": "BNC-1",
        "kind": "bench",
        "shelter_id": DESK,
        "ends": {"tx_a": BENCH["tx_a_enabled"], "tx_b": BENCH["tx_b_enabled"]},
        "turnaround": BENCH["turnaround_range"],
        "tones": BENCH["beacon_tones"],
        "log_slots": BENCH["log_slots"],
        "cold_reader_keeps_pending_frames": BENCH["cold_reader_keeps_pending_frames"],
        "loopback_control_consumes_response_slot": BENCH["loopback_control_consumes_response_slot"],
    }


def history_events():
    """Same historical originals for every scenario.

    RQ-1/RQ-2/RQ-3 are the issued transmission numbers; the shared log shows
    a single beep at the same_prefix slot and silence elsewhere; INT-1 is the
    one interruption: a desk action claimed at slot 5 but powered off, so the
    return slot there must not be defaulted to unoccupied.
    """
    return [
        {"id": "RQ-1", "kind": "beacon_request", "shelter_id": DESK, "slot": 0},
        {"id": "RQ-2", "kind": "beacon_request", "shelter_id": DESK, "slot": 2},
        {"id": "RQ-3", "kind": "beacon_request", "shelter_id": DESK, "slot": 3},
        {"id": "HB-0", "kind": "heartbeat", "shelter_id": DESK, "observed": "missing", "slot": 0, "reader": "shared"},
        {"id": "HB-1", "kind": "heartbeat", "shelter_id": DESK, "observed": "missing", "slot": 1, "reader": "shared"},
        {
            "id": "HB-2", "kind": "heartbeat", "shelter_id": DESK, "observed": "heard",
            "slot": SAME_PREFIX_SLOT, "reader": BENCH["same_prefix"]["reader"],
            "tone": SAME_PREFIX_TONE,
        },
        {"id": "HB-3", "kind": "heartbeat", "shelter_id": DESK, "observed": "missing", "slot": 3, "reader": "shared"},
        {"id": "INT-1", "kind": "interruption", "shelter_id": DESK, "slot": 5, "rewired": "unknown"},
    ]


def import_beacon(db, tmp_path, name, events):
    src = tmp_path / (name + ".json")
    src.write_text(json.dumps({"events": events}), encoding="utf-8")
    return import_file(db, "beacon-import", src)


def fresh_desk(tmp_path, tag=""):
    db = connect(":memory:")
    import_file(db, "shelter-import", FIXTURES / "shelter-ok.json")
    import_beacon(db, tmp_path, "history" + tag, [bench_event()] + history_events())
    return db


def plan_probe(db, tmp_path, tag, constraint, window=(4, 5)):
    return import_beacon(db, tmp_path, "probe" + tag, [
        {
            "id": "PRB" + tag, "kind": "probe", "shelter_id": DESK,
            "window": list(window), "constraint": constraint,
        }
    ])[0]["result"]


def followup(db):
    return report(db)["comms_followup"][0]["state"]


def requests(db):
    return {r["id"]: r for r in report(db)["beacon_request"]}


def work_order(db):
    rows = report(db)["beacon_work_order"]
    assert len(rows) == 1
    return rows[0]


def test_same_history_two_constraints_choose_different_captures(tmp_path):
    # constraint one: overwriting uncollected originals stays a hard rule,
    # so no window slot is legally wireable and the plan falls back to the
    # non-consuming cold reader.
    db = fresh_desk(tmp_path, "a")
    plan = plan_probe(db, tmp_path, "-a", "no_overwrite")
    assert plan["tool"] == "cold_reader"
    assert plan["points"] == [2, 3]
    assert plan["supported_ends"] == []
    assert plan["resolves_ambiguity"] is False
    assert any(NEW_END in gap for gap in plan["gaps"])
    assert set(plan["blocked_loopback_slots"]) == {"4", "5"}
    assert "unoccupied" in plan["blocked_loopback_slots"]["5"]
    assert plan["minimal_observations"]
    db.close()

    # constraint two: confirmation was received first, so the active test is
    # allowed and the sealed loopback label goes on the one usable slot.
    db = fresh_desk(tmp_path, "b")
    plan = plan_probe(db, tmp_path, "-b", "confirmed_active")
    assert plan["tool"] == "loopback"
    assert plan["slot"] == 4
    assert plan["label"] == "PRB-b-label"
    assert {c["request"] for c in plan["consumes"]} == {"RQ-2", "RQ-3"}
    assert plan["resolves_ambiguity"] is True
    assert plan["minimal_observations"] == ["tone of the wired response at slot 4"]
    db.close()


def test_cold_readback_attributes_delayed_answer_but_leaves_real_gap(tmp_path):
    db = fresh_desk(tmp_path)
    assert followup(db) == "request_readback"
    plan = plan_probe(db, tmp_path, "-1", "no_overwrite")
    result = import_beacon(db, tmp_path, "readback-1", [
        {
            "id": "RB-1", "kind": "readback", "shelter_id": DESK,
            "tool": "cold_reader", "points": plan["points"],
            "frames": [{"slot": SAME_PREFIX_SLOT, "end": OLD_END, "request_id": "RQ-1", "tone": SAME_PREFIX_TONE}],
        }
    ])[0]["result"]
    # the preserved frame proves the beep was the old unit's late answer to
    # RQ-1 (offset 2), so the new end is still unproven and the todo stays.
    assert result["answered"] == ["RQ-1"]
    assert result["followup"] == "request_readback"
    assert result["work_order"] == "pending"
    assert any(NEW_END in gap for gap in result["gaps"])
    reqs = requests(db)
    assert reqs["RQ-1"]["state"] == "answered"
    assert reqs["RQ-1"]["answer_end"] == OLD_END
    assert reqs["RQ-1"]["answer_offset"] == 2
    assert reqs["RQ-2"]["state"] == "outstanding"
    assert reqs["RQ-3"]["state"] == "outstanding"
    assert followup(db) == "request_readback"
    # slot 4 then passes silently: the remaining old originals expire and the
    # gap on the new end is final, which the audit trail must keep.
    import_beacon(db, tmp_path, "late-missing", [
        {"id": "HB-4", "kind": "heartbeat", "shelter_id": DESK, "observed": "missing", "slot": 4, "reader": "shared"}
    ])
    assert requests(db)["RQ-2"]["state"] == "expired"
    assert requests(db)["RQ-3"]["state"] == "expired"
    assert followup(db) == "request_readback"
    wo = work_order(db)
    assert wo["status"] == "pending"
    assert json.loads(wo["plan"])["tool"] == "cold_reader"
    states = {r["state"] for r in report(db)["decision_log"]}
    assert {"beacon_answered", "beacon_expired", "work_order_pending"} <= states
    assert audit(db, HANDLERS)["replay_ok"]
    db.close()


def test_loopback_readback_proves_new_end_and_consumes_pending_originals(tmp_path):
    db = fresh_desk(tmp_path)
    assert followup(db) == "request_readback"
    plan = plan_probe(db, tmp_path, "-2", "confirmed_active")
    result = import_beacon(db, tmp_path, "readback-2", [
        {
            "id": "RB-2", "kind": "readback", "shelter_id": DESK,
            "tool": "loopback", "slot": plan["slot"], "label": plan["label"],
            "responses": [{"end": NEW_END, "tone": NEW_TONE}],
        }
    ])[0]["result"]
    # the wired response proves the replacement end: the todo closes.
    assert result["followup"] == "monitor"
    assert result["work_order"] == "closed"
    assert result["proven"] == [NEW_END]
    assert followup(db) == "monitor"
    # the wiring spent slot 4: RQ-3 lost its last collectable original, while
    # RQ-1 and RQ-2 still share the single slot-2 beep (one beep, one number).
    assert result["consumed"] == ["RQ-3"]
    reqs = requests(db)
    assert reqs["RQ-3"]["state"] == "consumed"
    assert reqs["RQ-1"]["state"] == "outstanding"
    assert reqs["RQ-2"]["state"] == "outstanding"
    assert any("RQ-1" in gap and "RQ-2" in gap for gap in result["gaps"])
    proofs = report(db)["beacon_proof"]
    assert {(p["end"], p["kind"]) for p in proofs} == {(NEW_END, "loopback_response")}
    wo = work_order(db)
    assert wo["status"] == "closed"
    assert json.loads(wo["result"])["consumed"] == ["RQ-3"]
    states = {r["state"] for r in report(db)["decision_log"]}
    assert {"end_proven", "beacon_consumed", "work_order_closed"} <= states
    assert audit(db, HANDLERS)["replay_ok"]
    db.close()


def test_same_end_common_offset_is_enforced(tmp_path):
    db = fresh_desk(tmp_path)
    plan = plan_probe(db, tmp_path, "-3", "no_overwrite")
    forged = {
        "id": "RB-X", "kind": "readback", "shelter_id": DESK,
        "tool": "cold_reader", "points": plan["points"],
        "frames": [
            {"slot": 2, "end": OLD_END, "request_id": "RQ-1", "tone": SAME_PREFIX_TONE},
            {"slot": 3, "end": OLD_END, "request_id": "RQ-2", "tone": SAME_PREFIX_TONE},
        ],
    }
    # offsets 2 and 1 for the same end cannot both be true.
    with pytest.raises(ValueError, match="common offset"):
        import_beacon(db, tmp_path, "forged", [forged])
    assert requests(db)["RQ-1"]["state"] == "outstanding"
    assert requests(db)["RQ-2"]["state"] == "outstanding"
    assert followup(db) == "request_readback"
    assert work_order(db)["status"] == "planned"
    db.close()


def test_pending_original_cannot_be_consumed_twice(tmp_path):
    db = fresh_desk(tmp_path)
    plan = plan_probe(db, tmp_path, "-4", "no_overwrite")
    frame = {"slot": SAME_PREFIX_SLOT, "end": OLD_END, "request_id": "RQ-1", "tone": SAME_PREFIX_TONE}
    with pytest.raises(ValueError, match="twice"):
        import_beacon(db, tmp_path, "dup", [
            {
                "id": "RB-D", "kind": "readback", "shelter_id": DESK,
                "tool": "cold_reader", "points": plan["points"], "frames": [frame, dict(frame)],
            }
        ])
    import_beacon(db, tmp_path, "readback-4", [
        {
            "id": "RB-4", "kind": "readback", "shelter_id": DESK,
            "tool": "cold_reader", "points": plan["points"], "frames": [frame],
        }
    ])
    assert requests(db)["RQ-1"]["state"] == "answered"
    # the work order is still pending, but RQ-1's original is spent: a second
    # readback naming it must be rejected instead of consuming it again.
    with pytest.raises(ValueError, match="cannot be spent again"):
        import_beacon(db, tmp_path, "again", [
            {
                "id": "RB-5", "kind": "readback", "shelter_id": DESK,
                "tool": "cold_reader", "points": plan["points"], "frames": [frame],
            }
        ])
    assert followup(db) == "request_readback"
    assert audit(db, HANDLERS)["replay_ok"]
    db.close()


def test_cold_reader_stays_pending_when_new_end_never_requested(tmp_path):
    db = connect(":memory:")
    import_file(db, "shelter-import", FIXTURES / "shelter-ok.json")
    events = [
        bench_event(),
        {"id": "RQ-1", "kind": "beacon_request", "shelter_id": DESK, "slot": 0},
        {"id": "RQ-2", "kind": "beacon_request", "shelter_id": DESK, "slot": 1},
        {"id": "HB-0", "kind": "heartbeat", "shelter_id": DESK, "observed": "missing", "slot": 0, "reader": "shared"},
        {"id": "HB-1", "kind": "heartbeat", "shelter_id": DESK, "observed": "missing", "slot": 1, "reader": "shared"},
        {
            "id": "HB-2", "kind": "heartbeat", "shelter_id": DESK, "observed": "heard",
            "slot": SAME_PREFIX_SLOT, "reader": "shared", "tone": SAME_PREFIX_TONE,
        },
    ]
    import_beacon(db, tmp_path, "history-cold", events)
    # every surviving explanation of the beep names the old unit, so the old
    # end is supported by existing evidence while the new end stays unproven.
    assert followup(db) == "request_readback"
    assert {p["end"] for p in report(db)["beacon_proof"]} == {OLD_END}
    plan = plan_probe(db, tmp_path, "-cold", "no_overwrite", window=(3,))
    assert plan["tool"] == "cold_reader"
    assert plan["supported_ends"] == [OLD_END]
    assert plan["resolves_ambiguity"] is False
    assert any("never had a real request" in gap for gap in plan["gaps"])
    assert audit(db, HANDLERS)["replay_ok"]
    db.close()


def test_structured_evidence_requires_bench_and_plan(tmp_path):
    db = connect(":memory:")
    import_file(db, "shelter-import", FIXTURES / "shelter-ok.json")
    with pytest.raises(ValueError, match="declared bench"):
        import_beacon(db, tmp_path, "early", [
            {"id": "HB-X", "kind": "heartbeat", "shelter_id": DESK, "observed": "heard", "slot": 2, "reader": "shared", "tone": 1}
        ])
    import_beacon(db, tmp_path, "bench-only", [bench_event()])
    with pytest.raises(ValueError, match="slotted reading is required"):
        import_beacon(db, tmp_path, "plain", [
            {"id": "HB-Y", "kind": "heartbeat", "shelter_id": DESK, "observed": "heard"}
        ])
    db.close()
    db = fresh_desk(tmp_path, "g")
    with pytest.raises(ValueError, match="no open work order"):
        import_beacon(db, tmp_path, "unplanned", [
            {
                "id": "RB-Z", "kind": "readback", "shelter_id": DESK,
                "tool": "cold_reader", "points": [2, 3],
                "frames": [{"slot": 2, "end": OLD_END, "request_id": "RQ-1", "tone": 1}],
            }
        ])
    db.close()
