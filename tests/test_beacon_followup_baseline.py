import json
from pathlib import Path

from shelterledger.__main__ import HANDLERS, import_file
from shelterledger.report import audit, report
from shelterledger.store import connect

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_beacon_capture_drives_real_followup_and_audits(tmp_path):
    events = json.loads((FIXTURES / "beacon-desk-ok.json").read_text(encoding="utf-8"))["events"]
    db = connect(":memory:")
    try:
        import_file(db, "shelter-import", FIXTURES / "shelter-ok.json")
        for event, expected in zip(events, ("monitor", "request_readback")):
            src = tmp_path / (event["id"] + ".json")
            src.write_text(json.dumps({"events": [event]}), encoding="utf-8")
            assert import_file(db, "beacon-import", src)[0]["result"] == expected
            followup = report(db)["comms_followup"][0]
            assert followup["state"] == expected
            assert followup["observation_id"] == event["id"]
        assert audit(db, HANDLERS)["replay_ok"]
    finally:
        db.close()
