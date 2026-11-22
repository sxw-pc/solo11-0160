"""Acceptance tests for evidence-based crew work-time certificates.

Scope: two desks, at most three volunteers, three discrete time segments,
simulated paper originals. The same ten-plus-ten pair of sheets may describe
one wiring-team member seen twice (10 person-minutes) or two people working
simultaneously at different tool positions (20 person-minutes); the certificate
follows what the independently held field evidence can actually prove.
"""

import json
import pathlib
import tempfile
import unittest

try:
    import pytest
except ModuleNotFoundError:  # the graded environment runs pytest; keep a stdlib fallback
    class _Mark:
        def __getattr__(self, name):
            return lambda fn=None, *a, **k: fn if fn is not None else (lambda f: f)

    class _Raises:
        def __init__(self, etype, match=None):
            self.etype, self.match = etype, match

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            assert exc_type is not None and issubclass(exc_type, self.etype)
            if self.match:
                assert self.match in str(exc)
            return True

    class _Pytest:
        mark = _Mark()

        @staticmethod
        def fixture(fn=None, **kw):
            if fn is None:
                return lambda f: f
            return fn

        @staticmethod
        def raises(etype, match=None):
            return _Raises(etype, match)

    pytest = _Pytest()

from shelterledger.__main__ import HANDLERS, import_file
from shelterledger.crew_credit import certificate, next_shift_actions
from shelterledger.report import audit, report
from shelterledger.store import connect

TEAM = "utility-crew"


@pytest.fixture()
def db(tmp_path):
    conn = connect(tmp_path / "crew.sqlite")
    yield conn
    conn.close()


def send(db, tmp_path, events):
    path = tmp_path / "events.json"
    path.write_text(json.dumps({"events": events}))
    return import_file(db, "crew-import", path)


def sheets(claim=20, round_=1, start=0, stop=10):
    return [
        {
            "id": f"desk-left-r{round_}",
            "kind": "crew_window",
            "team": TEAM,
            "sheet_minutes": stop - start,
            "claim_minutes": claim,
            "witness": "desk-l",
            "start": start,
            "stop": stop,
            "desk": "left",
            "local_code": "L-1",
            "round": round_,
        },
        {
            "id": f"desk-right-r{round_}",
            "kind": "crew_window",
            "team": TEAM,
            "sheet_minutes": stop - start,
            "claim_minutes": claim,
            "witness": "desk-r",
            "start": start,
            "stop": stop,
            "desk": "right",
            "local_code": "R-1",
            "round": round_,
        },
    ]


def same_person_badge(eid="BA-1"):
    return {
        "id": eid,
        "kind": "badge_anchor",
        "team": TEAM,
        "claim_minutes": 20,
        "witness": "lead-hand",
        "badge_id": "badge-alpha",
        "issued_at": 0,
        "links": [
            {"desk": "left", "local_code": "L-1"},
            {
                "desk": "right",
                "local_code": "R-1",
                "same_as": [
                    {"desk": "left", "local_code": "L-1", "witnesses": ["desk-l", "desk-r"]}
                ],
            },
        ],
    }


def two_badges():
    return [
        {
            "id": "BA-L",
            "kind": "badge_anchor",
            "team": TEAM,
            "claim_minutes": 20,
            "witness": "lead-hand",
            "badge_id": "badge-alpha",
            "issued_at": 0,
            "links": [{"desk": "left", "local_code": "L-1"}],
        },
        {
            "id": "BA-R",
            "kind": "badge_anchor",
            "team": TEAM,
            "claim_minutes": 20,
            "witness": "deputy-hand",
            "badge_id": "badge-beta",
            "issued_at": 0,
            "links": [{"desk": "right", "local_code": "R-1"}],
        },
    ]


def station_pair():
    return [
        {
            "id": "SW-L",
            "kind": "station_witness",
            "team": TEAM,
            "claim_minutes": 20,
            "station": "station-left",
            "signed_at": 5,
            "covers_start": 0,
            "covers_stop": 10,
            "signatures": ["obs-a", "obs-b"],
            "simultaneous_occupancy": True,
        },
        {
            "id": "SW-R",
            "kind": "station_witness",
            "team": TEAM,
            "claim_minutes": 20,
            "station": "station-right",
            "signed_at": 5,
            "covers_start": 0,
            "covers_stop": 10,
            "signatures": ["obs-c", "obs-d"],
            "simultaneous_occupancy": True,
        },
    ]


def test_route_a_same_wiring_member_seen_twice_certifies_ten(db, tmp_path):
    outcomes = send(db, tmp_path, sheets() + [same_person_badge()])
    assert outcomes[-1]["result"] == "certified"
    cert = certificate(db, TEAM)
    assert cert["certified_minutes"] == 10
    assert cert["sheet_minutes"] == 20  # raw report summary kept
    assert cert["state"] == "certified" and cert["route"] == "A"
    assert cert["basis"]["identity_map"] == {"left:L-1": "badge-alpha", "right:R-1": "badge-alpha"}
    # two local paper codes resolve to one physical credential, not two people
    assert cert["basis"]["segments"][0]["route_a"]["people"] == 1


def test_route_a_two_distinct_anchors_certifies_twenty(db, tmp_path):
    send(db, tmp_path, sheets())
    outcomes = send(db, tmp_path, two_badges())
    cert = certificate(db, TEAM)
    assert outcomes[-1]["result"] == "certified"
    assert cert["certified_minutes"] == 20 and cert["route"] == "A"
    assert cert["basis"]["identity_map"] == {"left:L-1": "badge-alpha", "right:R-1": "badge-beta"}


def test_route_b_exclusive_stations_double_signed_certifies_twenty_without_badges(db, tmp_path):
    outcomes = send(db, tmp_path, sheets() + station_pair())
    cert = certificate(db, TEAM)
    assert outcomes[-1]["result"] == "certified"
    assert cert["certified_minutes"] == 20 and cert["route"] == "B"
    # who swapped which local code never has to be known
    assert cert["basis"]["identity_map"] == {}


def test_button_receipts_and_moved_station_cannot_stand_in_for_two_people(db, tmp_path):
    receipts = [
        {
            "id": f"BT-{side}",
            "kind": "button_receipt",
            "team": TEAM,
            "claim_minutes": 20,
            "station": f"station-{side}",
            "signed_at": 5,
            "covers_start": 0,
            "covers_stop": 10,
        }
        for side in ("left", "right")
    ]
    outcomes = send(db, tmp_path, sheets() + receipts)
    cert = certificate(db, TEAM)
    assert outcomes[-1]["result"] == "held"
    assert cert["certified_minutes"] == 0 and cert["state"] == "held"
    assert any("receipts" in item and "cannot stand in for two contributors" in item for item in cert["missing"])
    assert all(e["state"] == "insufficient" for e in report(db)["crew_evidence"])


def test_single_signature_station_witness_is_insufficient(db, tmp_path):
    weak = station_pair()
    weak[1]["signatures"] = ["obs-c"]
    send(db, tmp_path, sheets() + weak)
    cert = certificate(db, TEAM)
    assert cert["state"] == "held" and cert["certified_minutes"] == 0
    states = {e["id"]: e["state"] for e in report(db)["crew_evidence"]}
    assert states["SW-L"] == "recorded" and states["SW-R"] == "insufficient"


def test_routes_that_both_succeed_but_disagree_are_conflicts_not_max(db, tmp_path):
    send(db, tmp_path, sheets() + [same_person_badge()] + station_pair())
    cert = certificate(db, TEAM)
    assert cert["state"] == "conflict"
    assert cert["certified_minutes"] == 0  # never take the higher twenty
    conflict = cert["conflicts"][0]
    assert "mutually exclusive" in conflict["reason"]
    assert set(conflict["minimal_witnesses"]) == {"BA-1", "SW-L", "SW-R"}
    states = {e["id"]: e["state"] for e in report(db)["crew_evidence"]}
    assert all(states[eid] == "contradicted" for eid in ("BA-1", "SW-L", "SW-R"))
    # raw sheets and every original are retained, not deleted
    assert report(db)["crew_credit"][0]["sheet_minutes"] == 20
    assert len(report(db)["crew_evidence"]) == 3


def test_same_staffing_repeated_without_restart_witness_is_measured_once(db, tmp_path):
    events = sheets()
    events += sheets(round_=2)  # same interval, same local codes, no restart anchor
    events += [same_person_badge()]
    send(db, tmp_path, events)
    cert = certificate(db, TEAM)
    assert cert["state"] == "certified"
    assert cert["certified_minutes"] == 10  # not 20 from replaying the same hands
    seg = cert["basis"]["segments"][0]
    assert {w["window"] for w in seg["excluded"]} == {"desk-left-r2", "desk-right-r2"}
    assert any("no witnessed restart" in item for item in cert["missing"])


def test_restart_anchor_allows_a_second_round_to_count_again(db, tmp_path):
    events = sheets() + sheets(round_=2) + [same_person_badge()]
    events.append(
        {
            "id": "BA-2",
            "kind": "badge_anchor",
            "team": TEAM,
            "claim_minutes": 20,
            "witness": "lead-hand",
            "badge_id": "badge-alpha",
            "issued_at": 0,
            "round": 2,
            "links": [
                {"desk": "left", "local_code": "L-1"},
                {
                    "desk": "right",
                    "local_code": "R-1",
                    "same_as": [
                        {"desk": "left", "local_code": "L-1", "witnesses": ["desk-l", "desk-r"]}
                    ],
                },
            ],
        }
    )
    send(db, tmp_path, events)
    cert = certificate(db, TEAM)
    assert cert["certified_minutes"] == 20  # two witnessed rounds of one person, 10 each
    assert cert["basis"]["segments"][0]["excluded"] == []


def test_cross_slice_identity_picking_is_reported_with_minimal_witnesses(db, tmp_path):
    # slice one: L-1 and R-1 are the same badge. slice two: they are two badges.
    # Each slice looks independently provable; together the identity relations
    # cannot both hold, so the minimal conflicting anchors are surfaced.
    second_slice = [
        {
            "id": "desk-left-r1b",
            "kind": "crew_window",
            "team": TEAM,
            "sheet_minutes": 5,
            "claim_minutes": 30,
            "witness": "desk-l",
            "start": 10,
            "stop": 15,
            "desk": "left",
            "local_code": "L-1",
        },
        {
            "id": "desk-right-r1b",
            "kind": "crew_window",
            "team": TEAM,
            "sheet_minutes": 5,
            "claim_minutes": 30,
            "witness": "desk-r",
            "start": 10,
            "stop": 15,
            "desk": "right",
            "local_code": "R-1",
        },
    ]
    second_badges = [
        {
            "id": "BA-L2",
            "kind": "badge_anchor",
            "team": TEAM,
            "claim_minutes": 30,
            "witness": "lead-hand",
            "badge_id": "badge-alpha",
            "issued_at": 10,
            "links": [{"desk": "left", "local_code": "L-1"}],
        },
        {
            "id": "BA-R2",
            "kind": "badge_anchor",
            "team": TEAM,
            "claim_minutes": 30,
            "witness": "deputy-hand",
            "badge_id": "badge-beta",
            "issued_at": 10,
            "links": [{"desk": "right", "local_code": "R-1"}],
        },
    ]
    send(db, tmp_path, sheets(claim=30) + second_slice + [same_person_badge()] + second_badges)
    cert = certificate(db, TEAM)
    assert cert["state"] == "conflict"
    witnesses = {w for c in cert["conflicts"] for w in c["minimal_witnesses"]}
    assert witnesses <= {"BA-1", "BA-L2", "BA-R2"} and witnesses
    assert any("identity and station relation" in c["reason"] for c in cert["conflicts"])


def test_next_shift_backup_retrieval_certifies_and_is_allowed_once(db, tmp_path):
    send(db, tmp_path, sheets())
    backup = {
        "id": "BK-1",
        "kind": "backup_retrieval",
        "team": TEAM,
        "claim_minutes": 20,
        "station": "station-left",
        "signed_at": 5,
        "covers_start": 0,
        "covers_stop": 10,
        "signatures": ["obs-a", "obs-b"],
        "simultaneous_occupancy": True,
    }
    assert send(db, tmp_path, [backup])[-1]["result"] == "held"  # one station only
    second = dict(backup, id="BK-2", station="station-right", signatures=["obs-c", "obs-d"])
    with pytest.raises(ValueError, match="one read-back or re-sign"):
        send(db, tmp_path, [second])
    # the rejected late action leaves no quota record and the ledger stays replayable
    assert len(report(db)["crew_late_action"]) == 1


def test_late_simultaneous_witness_cannot_be_manufactured_after_period(db, tmp_path):
    send(db, tmp_path, sheets())
    late = {
        "id": "LATE-1",
        "kind": "station_witness",
        "team": TEAM,
        "claim_minutes": 20,
        "station": "station-left",
        "signed_at": 42,  # signed after the [0,10) work period
        "covers_start": 0,
        "covers_stop": 10,
        "signatures": ["obs-a", "obs-b"],
        "simultaneous_occupancy": True,
    }
    outcomes = send(db, tmp_path, [late])
    cert = certificate(db, TEAM)
    assert outcomes[-1]["result"] == "held"
    assert report(db)["crew_evidence"][0]["state"] == "insufficient"
    assert cert["certified_minutes"] == 0
    # a retrieved backup that was actually signed inside the period still works
    backup = {
        "id": "BK-1",
        "kind": "backup_retrieval",
        "team": TEAM,
        "claim_minutes": 20,
        "station": "station-left",
        "signed_at": 5,
        "covers_start": 0,
        "covers_stop": 10,
        "signatures": ["obs-a", "obs-b"],
        "simultaneous_occupancy": True,
    }
    assert send(db, tmp_path, [backup])[-1]["result"] == "held"


def test_witness_resign_backfills_identity_while_badge_root_exists(db, tmp_path):
    # anchor originally records only the left desk local code
    anchor = {
        "id": "BA-1",
        "kind": "badge_anchor",
        "team": TEAM,
        "claim_minutes": 20,
        "witness": "lead-hand",
        "badge_id": "badge-alpha",
        "issued_at": 0,
        "links": [{"desk": "left", "local_code": "L-1"}],
    }
    send(db, tmp_path, sheets() + [anchor])
    assert certificate(db, TEAM)["state"] == "held"
    resign = {
        "id": "RS-1",
        "kind": "witness_resign",
        "team": TEAM,
        "claim_minutes": 20,
        "target_evidence_id": "BA-1",
        "badge_id": "badge-alpha",
        "links": [
            {
                "desk": "right",
                "local_code": "R-1",
                "same_as": [
                    {"desk": "left", "local_code": "L-1", "witnesses": ["desk-l", "desk-r"]}
                ],
            }
        ],
    }
    assert send(db, tmp_path, [resign])[-1]["result"] == "certified"
    cert = certificate(db, TEAM)
    assert cert["certified_minutes"] == 10 and cert["route"] == "A"
    with pytest.raises(ValueError, match="one read-back or re-sign"):
        send(db, tmp_path, [dict(resign, id="RS-2")])


def test_later_real_work_order_overturns_an_earlier_certificate_and_keeps_history(db, tmp_path):
    send(db, tmp_path, sheets() + station_pair())
    first = certificate(db, TEAM)
    assert first["certified_minutes"] == 20 and first["certificate_version"] == 3
    # a later, truly held badge original proves both desks were one person
    send(db, tmp_path, [same_person_badge("BA-LATE")])
    current = certificate(db, TEAM)
    assert current["state"] == "conflict" and current["certified_minutes"] == 0
    assert current["certificate_version"] == 4
    history = report(db)["crew_certificate"]
    assert [row["version"] for row in history] == [1, 2, 3, 4]
    assert history[2]["certified_minutes"] == 20  # superseded certificate preserved
    states = {e["id"]: e["state"] for e in report(db)["crew_evidence"]}
    assert states["BA-LATE"] == "contradicted"
    assert states["SW-L"] == "contradicted" and states["SW-R"] == "contradicted"
    logs = [row["state"] for row in report(db)["decision_log"] if row["system"] == "crew"]
    assert logs[-1] == "conflict"


def test_replay_audit_is_deterministic_and_replays_never_borrow_other_shift_proof(db, tmp_path):
    send(db, tmp_path, sheets() + [same_person_badge()])
    assert audit(db, HANDLERS)["replay_ok"]
    # evidence captured for another shift scope cannot certify this shift
    other_windows = [dict(e, shift="shift-2", id=e["id"] + "-s2") for e in sheets()]
    other = [dict(e, shift="shift-2", id=e["id"] + "-s2") for e in station_pair()]
    send(db, tmp_path, other_windows + other)
    cert = certificate(db, TEAM)
    assert cert["state"] == "certified" and cert["certified_minutes"] == 10
    assert audit(db, HANDLERS)["replay_ok"]


def test_next_shift_actions_explain_what_each_capture_can_promise(db, tmp_path):
    send(db, tmp_path, sheets() + [same_person_badge()])
    actions = next_shift_actions(db, TEAM)
    by_action = {a["action"]: a for a in actions}
    assert by_action["fresh_simultaneous_occupancy_witness"]["available"] is False
    assert by_action["retrieve_independently_signed_backup"]["available"] is True
    assert "20 person-minutes" in json.dumps(by_action["retrieve_independently_signed_backup"]["outcomes"])
    send(
        db,
        tmp_path,
        [
            {
                "id": "BK-1",
                "kind": "backup_retrieval",
                "team": TEAM,
                "claim_minutes": 10,
                "station": "station-left",
                "signed_at": 5,
                "covers_start": 0,
                "covers_stop": 10,
                "signatures": ["obs-a", "obs-b"],
                "simultaneous_occupancy": True,
            }
        ],
    )
    assert all(a["available"] is False for a in next_shift_actions(db, TEAM) if a["action"] != "fresh_simultaneous_occupancy_witness")


def test_scope_caps_three_volunteers_and_three_segments(db, tmp_path):
    events = sheets()
    for n, badge in enumerate(("badge-alpha", "badge-beta", "badge-gamma", "badge-delta")):
        events.append(
            {
                "id": f"BA-{n}",
                "kind": "badge_anchor",
                "team": TEAM,
                "claim_minutes": 20,
                "witness": "lead-hand",
                "badge_id": badge,
                "issued_at": 0,
                "links": [{"desk": "left", "local_code": f"L-{n}"}],
            }
        )
    with pytest.raises(ValueError, match="three volunteers"):
        send(db, tmp_path, events)

    more_sheets = []
    for start in (10, 20, 30):
        extra = sheets(round_=1, start=start, stop=start + 5)
        for e in extra:
            e["id"] = f"{e['id']}-s{start}"
        more_sheets.extend(extra)
    with pytest.raises(ValueError, match="three discrete time segments"):
        send(db, tmp_path, sheets() + more_sheets)


def test_raw_report_summary_and_old_sheet_rows_remain_visible(db, tmp_path):
    claim_sheets = [dict(w, claim_minutes=10) for w in sheets(claim=10)]
    badge = dict(same_person_badge(), claim_minutes=10)
    send(db, tmp_path, claim_sheets + [badge])
    rows = report(db)["crew_window"]
    assert [row["minutes"] for row in rows] == [10, 10]
    assert all(row["shift"] == "shift-1" for row in rows)
    cert = certificate(db, TEAM)
    assert cert["basis"]["raw_sheet_minutes"] == 20
    assert cert["certified_minutes"] == 10
    assert cert["missing"] == []


class _CrewEvidenceFallback(unittest.TestCase):
    """Stdlib runner used only when pytest is unavailable; pytest collects the
    plain test_* functions above directly."""


def _install_fallback():
    import inspect

    if hasattr(pytest, "fixture") and pytest.__class__.__name__ != "_Pytest":
        return  # real pytest is present; do not shadow its collection
    tmp = pathlib.Path(tempfile.mkdtemp())
    for name, fn in list(globals().items()):
        if not name.startswith("test_") or not inspect.isfunction(fn):
            continue

        def make(fn):
            def test(self):
                conn = connect(tmp / f"{fn.__name__}.sqlite")
                try:
                    fn(conn, tmp)
                finally:
                    conn.close()

            test.__name__ = fn.__name__
            return test

        setattr(_CrewEvidenceFallback, name, make(fn))


_install_fallback()

if __name__ == "__main__":
    unittest.main()
