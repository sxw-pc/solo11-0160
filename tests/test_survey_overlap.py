"""Overlap-safe release of synthetic survey sums.

All seats are fictional internal marks; the public reply only ever
carries coverages and counts, never a seat list.
"""

import json
from pathlib import Path

import pytest

from shelterledger import survey_release
from shelterledger.__main__ import HANDLERS, import_file
from shelterledger.report import audit, report
from shelterledger.store import connect

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

# One true answer sheet consistent with the fixture: every kept or
# reported sum there is reproduced by these six synthetic 0/1 answers.
SHEET = {"a": 1, "b": 1, "c": 0, "d": 0, "e": 1, "f": 0}
ABC = ["a", "b", "c"]
ABCD = ["a", "b", "c", "d"]
DEF = ["d", "e", "f"]


def load_doc():
    return json.loads((FIXTURES / "survey_public_overlap.json").read_text(encoding="utf-8"))


def sheet_event(round_id):
    return {"id": f"SHEET-{round_id}", "kind": "sheet", "round_id": round_id, "answers": SHEET}


def posted_event(doc, round_id):
    kept = doc["kept_public"][0]
    return {
        "id": f"POST-{round_id}",
        "kind": "notice",
        "round_id": round_id,
        "sites": kept["sites"],
        "exact": kept["exact"],
    }


def request_event(doc, rid, round_id, candidates=None, triggers=None, groups="default"):
    event = {
        "id": rid,
        "kind": "request",
        "round_id": round_id,
        "candidates": candidates if candidates is not None else doc["candidate"],
        "protected": [doc["protected_position"]],
        "demand_triggers": triggers if triggers is not None else [doc["demand_trigger_at"]],
    }
    if groups == "default":
        groups = [doc["kept_public"][0]["sites"], doc["candidate"][1]["sites"]]
    if groups:
        event["groups"] = groups
    return event


def import_events(db, tmp_path, events, name):
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps({"events": events}), encoding="utf-8")
    return import_file(db, "survey-import", path)


def committed_sets(db):
    return sorted(
        (tuple(sorted(json.loads(row["sites"]))), row["lo"], row["hi"])
        for row in db.execute("SELECT sites,lo,hi FROM survey_notice ORDER BY rowid")
    )


def test_overlap_fixture_is_consistent_with_sheet():
    doc = load_doc()
    kept = doc["kept_public"][0]
    assert sum(SHEET[p] for p in kept["sites"]) == kept["exact"]
    for cand in doc["candidate"]:
        assert sum(SHEET[p] for p in cand["sites"]) == cand["reported"]
        # each tally alone clears the old three-person head-count habit
        assert len(cand["sites"]) >= 3


def test_first_release_safe_alone_but_overlap_combination_leaks():
    doc = load_doc()
    kept = doc["kept_public"][0]
    base = [(frozenset(kept["sites"]), kept["exact"], kept["exact"])]
    result = survey_release.assess(
        SHEET.keys(), base, doc["candidate"], [doc["protected_position"]],
        triggers=[doc["demand_trigger_at"]],
    )
    first, second = result["candidates"]
    # the second statistic alone would be safe next to the old notice ...
    assert second["individual_exact_safe"] is True
    # ... but the exact ABCD total already forces the protected seat to 0
    assert first["individual_exact_safe"] is False
    # and checking each statistic alone is not enough: both exacts together leak
    assert result["joint_exact_safe"] is False
    # so the first copy degrades to the tightest safe interval, never [0, all]
    assert first["release"] == {"kind": "interval", "lo": 2, "hi": 3}
    assert second["release"] == {"kind": "exact", "value": 1}


def test_two_procedures_approved_through_real_import(tmp_path):
    doc = load_doc()
    db = connect(":memory:")
    try:
        import_events(db, tmp_path, [sheet_event("wk-2"), posted_event(doc, "wk-2")], "history")
        outcome = import_events(db, tmp_path, [request_event(doc, "REQ-W2", "wk-2")], "request")[0]["result"]
        first, second = outcome["candidates"]
        # procedure two: original coverage, tightest interval, threshold truth
        assert first["release"] == {"kind": "interval", "lo": 2, "hi": 3}
        assert first["exact"] == {"publishable": False}
        assert first["demand"] == [
            {"trigger_at": 2, "answer": "at_least", "publishable": True, "bounds": [2, 4]}
        ]
        assert second["release"] == {"kind": "exact", "value": 1}
        assert second["demand"][0]["answer"] == "below"
        # procedure one: exact sums, but only of the fixed mutually exclusive groups
        assert outcome["groups"]["publishable"] is True
        assert outcome["groups"]["sums"] == [2, 1]
        assert outcome["groups"]["coverage_interval"] == [2, 3]  # no exact cross-group value
        assert outcome["state"] == "released"
        assert outcome["compromised"] == []
        assert outcome["joint_exact_safe"] is False
        assert outcome["joint_release_safe"] is True
        # the desk may promise: the old posted number is kept, interval and exact added
        promises = {(p["coverage"], p["lo"], p["hi"], p["provenance"]) for p in outcome["promises"]}
        assert (3, 2, 2, "posted") in promises
        assert (4, 2, 3, "released") in promises
        assert (3, 1, 1, "released") in promises
        notices = report(db)["survey_notice"]
        assert [n["provenance"] for n in notices] == ["posted", "released", "released"]
        assert audit(db, HANDLERS)["replay_ok"]
    finally:
        db.close()


def test_release_together_or_split_keeps_same_constraints(tmp_path):
    doc = load_doc()
    together = connect(":memory:")
    split = connect(":memory:")
    try:
        # both copies prepared in one request
        import_events(together, tmp_path, [sheet_event("wk-2"), posted_event(doc, "wk-2")], "t-history")
        import_events(together, tmp_path, [request_event(doc, "REQ-T", "wk-2")], "t-request")
        # the same copies released at separate times: the first is safe alone,
        # but the overlapping exact total can no longer follow it
        import_events(split, tmp_path, [sheet_event("wk-2"), posted_event(doc, "wk-2")], "s-history")
        first = import_events(
            split, tmp_path, [request_event(doc, "REQ-S1", "wk-2", candidates=doc["candidate"][1:])], "s-first"
        )[0]["result"]
        second = import_events(
            split, tmp_path, [request_event(doc, "REQ-S2", "wk-2", candidates=doc["candidate"][:1])], "s-second"
        )[0]["result"]
        assert first["candidates"][0]["release"] == {"kind": "exact", "value": 1}
        assert second["candidates"][0]["exact"] == {"publishable": False}
        assert second["candidates"][0]["release"] == {"kind": "interval", "lo": 2, "hi": 3}
        # same public constraints either way
        assert committed_sets(split) == committed_sets(together)
        for db in (together, split):
            # three provenance states coexist: original sheet, old posted
            # paper, and the newly released copies
            assert report(db)["survey_sheet"]
            assert {n["provenance"] for n in report(db)["survey_notice"]} == {"posted", "released"}
            # the protected seat keeps two feasible answers under all of it
            worlds = survey_release.feasible_worlds(sorted(SHEET), survey_release.committed(db))
            assert {w["d"] for w in worlds} == {0, 1}
        assert audit(together, HANDLERS)["replay_ok"]
        assert audit(split, HANDLERS)["replay_ok"]
    finally:
        together.close()
        split.close()


def test_future_overlapping_requests_cannot_break_promises(tmp_path):
    doc = load_doc()
    db = connect(":memory:")
    try:
        import_events(db, tmp_path, [sheet_event("wk-2"), posted_event(doc, "wk-2")], "history")
        import_events(db, tmp_path, [request_event(doc, "REQ-W2", "wk-2")], "request")
        import_events(db, tmp_path, [sheet_event("wk-3")], "sheet-w3")
        followup = {
            "id": "REQ-W3",
            "kind": "request",
            "round_id": "wk-3",
            "candidates": [
                {"sites": ["e", "f"], "reported": 1},  # overlaps DEF: would pin d to 0
                {"sites": ["a", "b", "c", "d", "e", "f"], "reported": 3},  # already implied
                {"sites": ABCD, "reported": 2},  # exact still not promisable, reformat or not
                {"sites": ["d"], "reported": 0},  # splitting the fixed group apart
            ],
            "protected": ["d"],
            "demand_triggers": [2],
        }
        out = import_events(db, tmp_path, [followup], "followup")[0]["result"]
        kinds = [c["release"]["kind"] for c in out["candidates"]]
        assert kinds == ["interval", "exact", "interval", "held"]
        # the overlapping exact sums stay out of reach however they are
        # phrased; only safe degradations are offered
        assert out["candidates"][0]["exact"] == {"publishable": False}
        assert out["candidates"][0]["release"] == {"kind": "interval", "lo": 0, "hi": 1}
        assert out["candidates"][2]["exact"] == {"publishable": False}
        assert out["candidates"][2]["release"] == {"kind": "interval", "lo": 2, "hi": 3}
        # the threshold promise stays available even though the exact value is held
        assert out["candidates"][2]["demand"][0] == {
            "trigger_at": 2, "answer": "at_least", "publishable": True, "bounds": [2, 4],
        }
        # old promises survive the refused requests; nothing is silently dropped
        promises = {(p["coverage"], p["lo"], p["hi"], p["provenance"]) for p in out["promises"]}
        assert (3, 2, 2, "posted") in promises
        assert (4, 2, 3, "released") in promises
        assert (3, 1, 1, "released") in promises
        assert all(p["continuable"] for p in out["promises"])
        assert out["joint_release_safe"] is True
        assert audit(db, HANDLERS)["replay_ok"]
    finally:
        db.close()


def test_compromised_history_is_marked_not_erased(tmp_path):
    db = connect(":memory:")
    try:
        import_events(db, tmp_path, [sheet_event("wk-4")], "sheet")
        # both totals were already posted on paper long ago; together they
        # prove the protected answer, and that cannot be un-posted
        import_events(db, tmp_path, [
            {"id": "POST-1", "kind": "notice", "round_id": "wk-4", "sites": ABC, "exact": 2},
            {"id": "POST-2", "kind": "notice", "round_id": "wk-4", "sites": ABCD, "exact": 2},
        ], "posted")
        req = {
            "id": "REQ-W4",
            "kind": "request",
            "round_id": "wk-4",
            "candidates": [{"sites": DEF, "reported": 1}],
            "protected": ["d"],
            "demand_triggers": [2],
        }
        out = import_events(db, tmp_path, [req], "request")[0]["result"]
        # the unrecoverable seat is named; withdrawal is not faked
        assert out["compromised"] == ["d"]
        # a new statistic that pins nothing further may still be released
        assert out["candidates"][0]["release"] == {"kind": "exact", "value": 1}
        notices = report(db)["survey_notice"]
        assert len(notices) == 3  # both old numbers are still kept as known constraints
        assert [n["provenance"] for n in notices] == ["posted", "posted", "released"]
        assert audit(db, HANDLERS)["replay_ok"]
    finally:
        db.close()


def test_demand_hints_and_intervals_stay_informative(tmp_path):
    doc = load_doc()
    db = connect(":memory:")
    try:
        import_events(db, tmp_path, [sheet_event("wk-2"), posted_event(doc, "wk-2")], "history")
        req = request_event(doc, "REQ-W2", "wk-2", triggers=[2, 3], groups=None)
        out = import_events(db, tmp_path, [req], "request")[0]["result"]
        first = out["candidates"][0]
        # never the lazy [0, all] interval while a tighter one is safe
        assert first["release"] == {"kind": "interval", "lo": 2, "hi": 3}
        hints = {h["trigger_at"]: h for h in first["demand"]}
        assert hints[2]["publishable"] is True
        assert hints[2]["answer"] == "at_least"
        # "below 3" would pin the total to exactly 2 and expose the protected seat
        assert hints[3]["publishable"] is False
        assert out["groups"] is None
        assert audit(db, HANDLERS)["replay_ok"]
    finally:
        db.close()


def test_request_replay_and_tamper_audit(tmp_path):
    doc = load_doc()
    db = connect(":memory:")
    try:
        import_events(db, tmp_path, [sheet_event("wk-2"), posted_event(doc, "wk-2")], "history")
        import_events(db, tmp_path, [request_event(doc, "REQ-W2", "wk-2")], "request")
        # replaying the same file changes nothing
        again = import_events(db, tmp_path, [request_event(doc, "REQ-W2", "wk-2")], "request")
        assert again[0]["result"] == "file_replayed"
        assert audit(db, HANDLERS)["replay_ok"]
        # the truly published numbers are pinned; tampering is caught
        db.execute("UPDATE survey_notice SET hi=3 WHERE notice_id='POST-wk-2'")
        db.commit()
        assert "survey_notice" in audit(db, HANDLERS)["mismatches"]
    finally:
        db.close()


def test_capture_validation_guards(tmp_path):
    db = connect(":memory:")
    try:
        # every seat answers a single 0 or 1
        bad_bit = {"id": "S1", "kind": "sheet", "round_id": "bad", "answers": {"a": 2}}
        with pytest.raises(ValueError):
            import_events(db, tmp_path, [bad_bit], "bad1")
        # at most six seats
        big = {"id": "S2", "kind": "sheet", "round_id": "big", "answers": {str(i): 1 for i in range(7)}}
        with pytest.raises(ValueError):
            import_events(db, tmp_path, [big], "bad2")
        # a release request needs the original answer sheet first
        orphan = {
            "id": "R1", "kind": "request", "round_id": "none",
            "candidates": [{"sites": ["a"], "reported": 0}], "protected": [],
        }
        with pytest.raises(ValueError):
            import_events(db, tmp_path, [orphan], "bad3")
        import_events(db, tmp_path, [sheet_event("wk-5")], "sheet5")
        # at most two registered thresholds
        many_triggers = {
            "id": "R2", "kind": "request", "round_id": "wk-5",
            "candidates": [{"sites": ABC, "reported": 2}], "protected": [],
            "demand_triggers": [1, 2, 3],
        }
        with pytest.raises(ValueError):
            import_events(db, tmp_path, [many_triggers], "bad4")
        # groups must be mutually exclusive in one evidence
        overlapping = {
            "id": "R3", "kind": "request", "round_id": "wk-5",
            "candidates": [{"sites": ABC, "reported": 2}], "protected": [],
            "groups": [["a", "b"], ["b", "c"]],
        }
        with pytest.raises(ValueError):
            import_events(db, tmp_path, [overlapping], "bad5")
        # a reported sum that contradicts the sheet is refused
        wrong_sum = {
            "id": "R4", "kind": "request", "round_id": "wk-5",
            "candidates": [{"sites": ABC, "reported": 3}], "protected": [],
        }
        with pytest.raises(ValueError):
            import_events(db, tmp_path, [wrong_sum], "bad6")
        # a posted notice that contradicts the sheet is refused
        wrong_notice = {"id": "N0", "kind": "notice", "round_id": "wk-5", "sites": ABC, "exact": 3}
        with pytest.raises(ValueError):
            import_events(db, tmp_path, [wrong_notice], "bad7")
        # a second, different sheet for the same round is a version conflict
        other_sheet = {"id": "S3", "kind": "sheet", "round_id": "wk-5", "answers": dict(SHEET, d=1)}
        with pytest.raises(ValueError):
            import_events(db, tmp_path, [other_sheet], "bad8")
        # at most three notices per round
        import_events(db, tmp_path, [
            {"id": "N1", "kind": "notice", "round_id": "wk-5", "sites": ABC, "exact": 2},
            {"id": "N2", "kind": "notice", "round_id": "wk-5", "sites": DEF, "exact": 1},
            {"id": "N3", "kind": "notice", "round_id": "wk-5", "sites": ABCD, "at_least": 2},
        ], "notices")
        fourth = {"id": "N4", "kind": "notice", "round_id": "wk-5", "sites": ["a"], "at_most": 1}
        with pytest.raises(ValueError):
            import_events(db, tmp_path, [fourth], "bad9")
    finally:
        db.close()


def test_old_tally_entry_still_works_alongside(tmp_path):
    db = connect(":memory:")
    try:
        events = json.loads((FIXTURES / "survey-tallies-ok.json").read_text(encoding="utf-8"))["events"]
        out = import_events(db, tmp_path, events, "tallies")
        assert [r["result"] for r in out] == ["publishable", "held"]
        rows = report(db)["survey_release"]
        assert len(rows) == 1 and rows[0]["state"] == "held"  # same round reconsidered
        assert audit(db, HANDLERS)["replay_ok"]
    finally:
        db.close()
