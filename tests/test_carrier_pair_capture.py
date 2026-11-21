"""Evidence rules for the paired empty-carrier label capture."""

import json

import pytest

from shelterledger.__main__ import HANDLERS, import_file
from shelterledger.carrier_pair_capture import CaptureError, receipt, show
from shelterledger.report import audit, report
from shelterledger.store import connect, one

FIXTURES = __import__("pathlib").Path(__file__).resolve().parents[1] / "fixtures"
DESK = "hall-a"


def reading(code, page_side="outer"):
    return {"code": code, "page_side": page_side}


def image(image_id, lane, *readings, position=None, orientation="straight", serial=None):
    payload = {"image_id": image_id, "lane": lane, "readings": list(readings)}
    if position is not None:
        payload["position"] = position
    if orientation is not None:
        payload["orientation"] = orientation
    if serial is not None:
        payload["envelope_serial"] = serial
    return payload


def pair_round(round_id, *images, orientation="straight", shared_stop=True, witness="site-w1"):
    return {
        "id": f"PR-{round_id}",
        "kind": "pair-round",
        "shelter_id": DESK,
        "round_id": round_id,
        "mode": "paired_fixed_camera",
        "turntable": {"houses": 2, "shared_stop": shared_stop},
        "binding": {"witness": witness, "round_id": round_id,
                    "orientation": orientation, "sides": "both"},
        "images": list(images),
    }


def envelope_round(round_id, *images):
    return {
        "id": f"PR-{round_id}",
        "kind": "pair-round",
        "shelter_id": DESK,
        "round_id": round_id,
        "mode": "separate_envelopes",
        "images": list(images),
    }


@pytest.fixture
def db(tmp_path):
    handle = connect(tmp_path / "carrier.sqlite")
    import_file(handle, "shelter-import", FIXTURES / "shelter-ok.json")
    try:
        yield handle
    finally:
        handle.close()


def feed(db, events, command="carrier-import", tmp_path=None, name="packet.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"events": events}), encoding="utf-8")
    return import_file(db, command, path)


def slot(db, position):
    row = one(db, "SELECT state, reading_id FROM carrier_sleeve WHERE slot=?", (position,))
    return (row["state"], row["reading_id"]) if row else None


# --- the original independent reader keeps working --------------------------

def test_original_ready_then_needs_capture_path_is_unchanged(tmp_path):
    db = connect(tmp_path / "base.sqlite")
    try:
        import_file(db, "shelter-import", FIXTURES / "shelter-ok.json")
        packet = json.loads((FIXTURES / "carrier-read-ok.json").read_text(encoding="utf-8"))
        for event, expected in zip(packet["events"], ("ready", "needs_capture")):
            alone = tmp_path / (event["id"] + ".json")
            alone.write_text(json.dumps({"events": [event]}), encoding="utf-8")
            assert import_file(db, "carrier-import", alone)[0]["result"] == expected
            assert report(db)["carrier_sleeve"][0]["state"] == expected
        assert audit(db, HANDLERS)["replay_ok"]
    finally:
        db.close()


def test_pair_capture_does_not_disturb_the_independent_reader(db, tmp_path):
    feed(db, [{"id": "L-I", "kind": "label-read", "slot": "desk-position-one",
               "shelter_id": DESK, "labels_seen": ["v7"]}], tmp_path=tmp_path)
    feed(db, [pair_round("R1", image("A1", "left", reading("v7")),
                         image("A2", "right", reading("v8")))],
         tmp_path=tmp_path, name="pair.json")
    assert slot(db, "desk-position-one") == ("ready", "L-I")
    assert slot(db, "left") == ("ready", "R1")
    assert slot(db, "right") == ("ready", "R1")


# --- legal capture one: paired fixed cameras on one turntable stop ----------

def test_paired_fixed_cameras_on_one_stop_issue_both_sleeves(db, tmp_path):
    result = feed(db, [pair_round(
        "R1",
        image("A1", "left", reading("v7")),
        image("A2", "right", reading("v8", page_side="inner_reflection")),
        image("A3", "right", reading("v8")),
    )], tmp_path=tmp_path)[0]["result"]
    assert [o["action"] for o in result["outcomes"]] == ["issued", "issued"]
    assert {(s["position"], s["code"]) for s in result["sleeves"]} == {("left", "v7"), ("right", "v8")}
    assert slot(db, "left") == ("ready", "R1")
    assert slot(db, "right") == ("ready", "R1")
    assert result["next_action"]["action"] == "done"
    row = one(db, "SELECT * FROM carrier_pair_round WHERE round_id='R1'")
    assert row["binding_orientation"] == "straight"
    assert row["binding_sides"] == "both"
    assert audit(db, HANDLERS)["replay_ok"]


def test_inner_reflection_never_assigns_a_side(db, tmp_path):
    """The reflected inner page is catalogue content, not positional evidence."""
    result = feed(db, [pair_round(
        "R1",
        image("A1", "left", reading("v7")),
        image("A2", "right", reading("v8", page_side="inner_reflection")),
    )], tmp_path=tmp_path)[0]["result"]
    assert [o["action"] for o in result["outcomes"]] == ["issued"]
    assert result["sleeves"] == [{"code": "v7", "position": "left", "round_id": "R1",
                                  "witness": "paired_fixed_camera"}]
    assert result["outcomes"][0]["pending"]["position"] == "right"
    assert slot(db, "right") is None


def test_label_order_inside_one_frame_cannot_pick_a_side(db, tmp_path):
    """Both outer readings name v7, so neither edge may be handed v8."""
    result = feed(db, [pair_round(
        "R1", image("A1", "left", reading("v7")),
        image("A2", "right", reading("v7"), reading("v8", page_side="inner_reflection")),
    )], tmp_path=tmp_path)[0]["result"]
    assert not result["sleeves"]
    assert result["outcomes"][0]["action"] == "blocked"
    assert result["next_action"]["action"] == "do_not_split_yet"
    assert slot(db, "left") is None and slot(db, "right") is None


def test_two_outer_codes_on_one_edge_block_instead_of_guessing(db, tmp_path):
    result = feed(db, [pair_round(
        "R1",
        image("A1", "left", reading("v7")),
        image("A2", "right", reading("v7"), reading("v8")),
    )], tmp_path=tmp_path)[0]["result"]
    assert result["outcomes"][0]["action"] == "blocked"
    assert not result["sleeves"]
    assert result["next_action"]["action"] == "do_not_split_yet"


def test_overexposed_edge_is_not_filled_from_the_other_camera(db, tmp_path):
    result = feed(db, [pair_round(
        "R1",
        image("A1", "left", reading("v7")),
        image("A2", "right", reading("v7", page_side="inner_reflection")),
    )], tmp_path=tmp_path)[0]["result"]
    assert [o["action"] for o in result["outcomes"]] == ["issued"]
    assert result["sleeves"] == [{"code": "v7", "position": "left", "round_id": "R1",
                                  "witness": "paired_fixed_camera"}]
    pending = result["outcomes"][0]["pending"]
    assert pending["position"] == "right"
    assert pending["status"] == "unconfirmed"
    assert pending["candidate_codes"] == []
    assert slot(db, "right") is None
    assert result["next_action"]["action"] == "re_witness_pending_side"


def test_two_screenshots_from_different_stops_are_not_mirrored(db, tmp_path):
    event = pair_round(
        "R1",
        image("A1", "left", reading("v7"), orientation="straight"),
        image("A2", "right", reading("v7"), reading("v8"), orientation="turned"),
    )
    with pytest.raises(CaptureError):
        feed(db, [event], tmp_path=tmp_path)
    assert report(db)["carrier_pair_round"] == []
    assert slot(db, "left") is None and slot(db, "right") is None


def test_lost_shared_stop_witness_issues_nothing(db, tmp_path):
    result = feed(db, [pair_round(
        "R1", image("A1", "left", reading("v7")), image("A2", "right", reading("v8")),
        shared_stop=False,
    )], tmp_path=tmp_path)[0]["result"]
    assert result["round"]["state"] == "interrupted"
    assert result["round"]["interruption"] == "shared_stop_not_witnessed"
    assert result["outcomes"][0]["action"] == "hold"
    assert slot(db, "left") is None and slot(db, "right") is None
    assert audit(db, HANDLERS)["replay_ok"]


def test_binding_must_name_the_same_stop_as_the_images(db, tmp_path):
    event = pair_round("R1", image("A1", "left", reading("v7"), orientation="turned"),
                       image("A2", "right", reading("v8"), orientation="turned"),
                       orientation="straight")
    with pytest.raises(CaptureError):
        feed(db, [event], tmp_path=tmp_path)
    assert report(db)["carrier_pair_round"] == []


def test_binding_for_another_round_or_partial_side_is_rejected(db, tmp_path):
    bad_round = pair_round("R1", image("A1", "left", reading("v7")),
                           image("A2", "right", reading("v8")))
    bad_round["binding"]["round_id"] = "R9"
    with pytest.raises(CaptureError):
        feed(db, [bad_round], tmp_path=tmp_path)
    bad_sides = pair_round("R2", image("A1", "left", reading("v7")),
                           image("A2", "right", reading("v8")))
    bad_sides["binding"]["sides"] = "left"
    with pytest.raises(CaptureError):
        feed(db, [bad_sides], tmp_path=tmp_path)
    assert report(db)["carrier_pair_round"] == []


# --- a late image may report, never rewrite --------------------------------

def test_late_image_cannot_flip_an_issued_sleeve(db, tmp_path):
    feed(db, [pair_round("R1", image("A1", "left", reading("v7")),
                         image("A2", "right", reading("v8")))], tmp_path=tmp_path)
    late = feed(db, [{"id": "LI", "kind": "pair-image", "round_id": "R1",
                      "image": image("A3", "left", reading("v8"), orientation="straight")}],
                tmp_path=tmp_path, name="late.json")[0]["result"]
    assert late["outcomes"][0]["action"] == "rejected"
    assert {(s["position"], s["code"]) for s in receipt(db)["sleeves"]} == {
        ("left", "v7"), ("right", "v8")}
    assert one(db, "SELECT COUNT(*) AS n FROM carrier_image WHERE round_id='R1'")["n"] == 2
    assert audit(db, HANDLERS)["replay_ok"]


def test_late_image_can_only_confirm_an_unconfirmed_position(db, tmp_path):
    """A late frame reports the pending edge but never rewrites the printed left."""
    feed(db, [pair_round("R1", image("A1", "left", reading("v7")),
                         image("A2", "right", reading("v8", page_side="inner_reflection")))],
         tmp_path=tmp_path)
    assert slot(db, "left") == ("ready", "R1") and slot(db, "right") is None
    shown = show(db, "R1")
    assert shown["outcomes"][0]["action"] == "issued"
    assert shown["outcomes"][0]["pending"]["position"] == "right"
    # The late image is filed as evidence only; the pending right edge stays
    # unconfirmed because a second capture on the same stop is not re-run.
    feed(db, [{"id": "LI", "kind": "pair-image", "round_id": "R1",
               "image": image("A3", "right", reading("v8"))}],
         tmp_path=tmp_path, name="late.json")
    assert slot(db, "left") == ("ready", "R1")
    assert one(db, "SELECT code FROM carrier_code_position WHERE position='left'")["code"] == "v7"
    assert slot(db, "right") is None


# --- legal capture two: one sealed envelope after one real split ------------

def split(**over):
    event = {"id": "SP1", "kind": "split-start", "round_id": "R1", "witness": "site-w2"}
    event.update(over)
    return event


def receipt_event(**over):
    event = {"id": "RC1", "kind": "split-receipt", "round_id": "R1", "position": "left",
             "code": "v7", "envelope_serial": "ENV-1", "witness": "site-w2"}
    event.update(over)
    return event


def test_split_then_one_receipt_issues_exactly_one_sleeve(db, tmp_path):
    # The shared-stop witness was lost, so the paired round prints nothing;
    # the real split is then still available.
    feed(db, [pair_round("R1", image("A1", "left", reading("v7")),
                         image("A2", "right", reading("v8")), shared_stop=False)],
         tmp_path=tmp_path)
    assert not report(db)["carrier_sleeve"]
    assert feed(db, [split()], tmp_path=tmp_path, name="split.json")[0]["result"] == "split_open"
    result = feed(db, [receipt_event()], tmp_path=tmp_path, name="receipt.json")[0]["result"]
    assert result["next_action"]["action"] == "capture_sealed_envelope"
    # Only the sealed envelope is photographed; the paired source is gone.
    result = feed(db, [envelope_round("R2", image("B1", "both", reading("v7"),
                                                  serial="ENV-1"))],
                  tmp_path=tmp_path, name="env.json")[0]["result"]
    assert [o["action"] for o in result["outcomes"]] == ["issued"]
    assert result["sleeves"] == [{"code": "v7", "position": "left", "round_id": "R2",
                                  "witness": "separate_envelopes"}]
    pending = result["outcomes"][0]["pending"]
    assert pending["position"] == "right"
    assert pending["status"] == "unconfirmed"
    assert slot(db, "right") is None
    # The unconfirmed edge stays open forever: a late paired frame cannot join.
    assert result["next_action"]["action"] == "hold_unconfirmed_position"
    assert audit(db, HANDLERS)["replay_ok"]


def test_two_rounds_from_different_stops_cannot_complete_a_pair(db, tmp_path):
    """A printed sleeve from one stop is not completed by a second screenshot."""
    first = feed(db, [pair_round(
        "R1", image("A1", "left", reading("v7")),
        image("A2", "right", reading("v7"), reading("v8", page_side="inner_reflection")),
    )], tmp_path=tmp_path, name="r1.json")[0]["result"]
    # "Both outer readings say v7" is blocked, so nothing was printed.
    assert first["outcomes"][0]["action"] == "blocked"
    assert not first["sleeves"]
    second = feed(db, [pair_round(
        "R2", image("B1", "left", reading("v7"), orientation="turned"),
        image("B2", "right", reading("v8"), orientation="turned"), orientation="turned",
    )], tmp_path=tmp_path, name="r2.json")[0]["result"]
    # The second round may print its own pair (it is a genuine shared stop)...
    assert second["round"]["binding_orientation"] == "turned"
    assert {(s["position"], s["code"]) for s in second["sleeves"]} == {
        ("left", "v7"), ("right", "v8")}
    # ...but nothing from the first round is reused to add a mirror.
    assert second["outcomes"][0]["images"] == ["B1"]


def test_left_only_round_cannot_be_filled_by_a_later_right_only_round(db, tmp_path):
    feed(db, [pair_round(
        "R1", image("A1", "left", reading("v7")),
        image("A2", "right", reading("v8", page_side="inner_reflection")),
    )], tmp_path=tmp_path)
    assert slot(db, "left") == ("ready", "R1") and slot(db, "right") is None
    result = feed(db, [pair_round(
        "R2", image("B1", "right", reading("v8")),
        image("B2", "left", reading("v7", page_side="inner_reflection")),
    )], tmp_path=tmp_path, name="r2.json")[0]["result"]
    assert result["outcomes"][0]["action"] == "blocked"
    assert "one witnessed turntable stop" in result["outcomes"][0]["reason"]
    assert slot(db, "right") is None


def test_several_turntable_states_can_coexist_before_any_issue(db, tmp_path):
    """The first action must hold while both left/right worlds are still possible."""
    result = feed(db, [pair_round(
        "R1", image("A1", "left", reading("v7")),
        image("A2", "right", reading("v7"), reading("v8", page_side="inner_reflection")),
    )], tmp_path=tmp_path)[0]["result"]
    # v8 lives only in a reflection, so neither "v8 on the right" nor
    # "v7 on the right" is proven; the desk refuses to choose and prints nothing.
    assert result["outcomes"][0]["action"] == "blocked"
    assert result["next_action"]["action"] == "do_not_split_yet"


def test_receipt_empty_of_a_confirming_image_holds(db, tmp_path):
    feed(db, [pair_round("R1", image("A1", "left", reading("v7")),
                         image("A2", "right", reading("v8")), shared_stop=False)],
         tmp_path=tmp_path)
    feed(db, [split()], tmp_path=tmp_path, name="split.json")
    feed(db, [receipt_event()], tmp_path=tmp_path, name="receipt.json")
    result = feed(db, [envelope_round("R2", image("B1", "both", reading("v9"),
                                                  serial="ENV-1"))],
                  tmp_path=tmp_path, name="env.json")[0]["result"]
    # Same envelope serial but a different code: the sealed plate cannot be
    # relabelled, so the desk blocks instead of printing.
    assert result["outcomes"][0]["action"] == "blocked"
    assert not result["sleeves"]


def test_receipt_with_no_matching_image_holds(db, tmp_path):
    feed(db, [pair_round("R1", image("A1", "left", reading("v7")),
                         image("A2", "right", reading("v8")), shared_stop=False)],
         tmp_path=tmp_path)
    feed(db, [split()], tmp_path=tmp_path, name="split.json")
    feed(db, [receipt_event()], tmp_path=tmp_path, name="receipt.json")
    result = feed(db, [envelope_round("R2", image("B1", "both", reading("v9"),
                                                  serial="ENV-9"))],
                  tmp_path=tmp_path, name="env.json")[0]["result"]
    assert result["outcomes"][0]["action"] == "hold"
    assert not result["sleeves"]


def test_sealed_round_requires_the_one_split(db, tmp_path):
    with pytest.raises(CaptureError):
        feed(db, [envelope_round("R1", image("B1", "both", reading("v7"), serial="ENV-1"))],
             tmp_path=tmp_path)
    assert report(db)["carrier_pair_round"] == []


def test_interrupted_split_is_not_conditionally_repeated(db, tmp_path):
    # A round that prints nothing, so the one split is still available.
    feed(db, [pair_round("R1", image("A1", "left", reading("v7")),
                         image("A2", "right", reading("v7"), reading("v8", page_side="inner_reflection")))],
         tmp_path=tmp_path)
    assert not report(db)["carrier_sleeve"]
    feed(db, [split()], tmp_path=tmp_path, name="split.json")
    result = feed(db, [{"id": "SI", "kind": "split-interrupt", "round_id": "R1",
                        "detail": "cage may already be moved"}],
                  tmp_path=tmp_path, name="interrupt.json")[0]["result"]
    assert result == "split_interrupted"
    # No electronic receipt can be invented after the trays may have moved.
    with pytest.raises(CaptureError):
        feed(db, [receipt_event()], tmp_path=tmp_path, name="receipt.json")
    # And a second split round is refused regardless of electronic state.
    with pytest.raises(CaptureError):
        feed(db, [split(id="SP2", witness="site-w3")], tmp_path=tmp_path, name="split2.json")
    with pytest.raises(CaptureError):
        feed(db, [envelope_round("R2", image("B1", "both", reading("v7"), serial="ENV-1"))],
             tmp_path=tmp_path, name="env.json")
    assert slot(db, "left") is None and slot(db, "right") is None
    assert audit(db, HANDLERS)["replay_ok"]


def test_split_after_a_printed_sleeve_is_refused(db, tmp_path):
    feed(db, [pair_round("R1", image("A1", "left", reading("v7")),
                         image("A2", "right", reading("v8")))], tmp_path=tmp_path)
    assert slot(db, "left") == ("ready", "R1")
    with pytest.raises(CaptureError):
        feed(db, [split()], tmp_path=tmp_path, name="split.json")
    assert slot(db, "left") == ("ready", "R1") and slot(db, "right") == ("ready", "R1")


# --- limits: two carriers, two rounds, four originals -----------------------

def test_only_two_rounds_and_four_images(db, tmp_path):
    feed(db, [pair_round("R1", image("A1", "left", reading("v7")),
                         image("A2", "right", reading("v7"), reading("v8", page_side="inner_reflection")))],
         tmp_path=tmp_path)
    # Two rounds are permitted here; the third is refused.
    feed(db, [pair_round("R2", image("A3", "left", reading("v7")),
                         image("A4", "right", reading("v8")))],
         tmp_path=tmp_path, name="r2.json")
    with pytest.raises(CaptureError):
        feed(db, [pair_round("R3", image("A5", "left", reading("v7")),
                             image("A6", "right", reading("v8")))],
             tmp_path=tmp_path, name="r3.json")
    # Four originals are already on file; a fifth is refused.
    with pytest.raises(CaptureError):
        feed(db, [{"id": "LI", "kind": "pair-image", "round_id": "R2",
                   "image": image("A5", "left", reading("v8"))}],
             tmp_path=tmp_path, name="late.json")
    assert one(db, "SELECT COUNT(*) AS n FROM carrier_image")["n"] == 4


def test_carriers_must_be_exactly_two(db, tmp_path):
    event = pair_round("R1", image("A1", "left", reading("v7")),
                       image("A2", "right", reading("v8")))
    event["turntable"]["houses"] = 3
    with pytest.raises(CaptureError):
        feed(db, [event], tmp_path=tmp_path)
    assert report(db)["carrier_pair_round"] == []


def test_duplicate_code_on_both_edges_is_blocked(db, tmp_path):
    result = feed(db, [pair_round("R1", image("A1", "left", reading("v7")),
                                  image("A2", "right", reading("v7")))],
                  tmp_path=tmp_path)[0]["result"]
    assert result["outcomes"][0]["action"] == "blocked"
    assert not result["sleeves"]


def test_fixtures_show_one_pass_and_one_refusal(db, tmp_path):
    ok = import_file(db, "carrier-import", FIXTURES / "carrier-pair-ok.json")
    assert ok[0]["result"]["sleeves"] == [
        {"code": "v7", "position": "left", "round_id": "R1", "witness": "paired_fixed_camera"},
        {"code": "v8", "position": "right", "round_id": "R1", "witness": "paired_fixed_camera"},
    ]
    with pytest.raises(CaptureError):
        import_file(db, "carrier-import", FIXTURES / "carrier-pair-bad.json")
    # The bad frame came from two different stops, so nothing was added.
    assert len(report(db)["carrier_image"]) == 2


def test_late_image_after_a_full_pair_is_archived_only(db, tmp_path):
    feed(db, [pair_round("R1", image("A1", "left", reading("v7")),
                         image("A2", "right", reading("v8")))], tmp_path=tmp_path)
    assert slot(db, "left") == ("ready", "R1") and slot(db, "right") == ("ready", "R1")
    result = feed(db, [{"id": "LI", "kind": "pair-image", "round_id": "R1",
                        "image": image("A3", "both", reading("v7"), reading("v8"))}],
                  tmp_path=tmp_path, name="late.json")[0]["result"]
    # The image is on file, but it bound nothing and changed no code.
    assert one(db, "SELECT late FROM carrier_image WHERE image_id='A3'")["late"] == 1
    assert {(s["position"], s["code"]) for s in receipt(db)["sleeves"]} == {
        ("left", "v7"), ("right", "v8")}
    assert result["next_action"]["action"] == "done"
    assert audit(db, HANDLERS)["replay_ok"]


def test_replay_is_idempotent_and_audit_replays_history(db, tmp_path):
    packet = pair_round("R1", image("A1", "left", reading("v7")),
                        image("A2", "right", reading("v8")))
    feed(db, [packet], tmp_path=tmp_path, name="a.json")
    # The same file bytes are a replay, not a second issue.
    assert import_file(db, "carrier-import", tmp_path / "a.json") == [{"result": "file_replayed"}]
    # A byte-different file carrying the same event hits the immutable identity.
    clone = dict(packet)
    clone_path = tmp_path / "b.json"
    clone_path.write_text(json.dumps({"events": [clone], "note": "other copy"}), encoding="utf-8")
    result = import_file(db, "carrier-import", clone_path)
    assert result[0]["result"] == "event_replayed"
    assert audit(db, HANDLERS)["replay_ok"]
    assert report(db)["carrier_sleeve"] == [
        {"slot": "left", "state": "ready", "reading_id": "R1"},
        {"slot": "right", "state": "ready", "reading_id": "R1"},
    ]
