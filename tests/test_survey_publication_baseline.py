import json
from pathlib import Path

from shelterledger.__main__ import HANDLERS, import_file
from shelterledger.report import audit, report
from shelterledger.store import connect

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_tally_capture_reconsiders_publication(tmp_path):
    events = json.loads((FIXTURES / "survey-tallies-ok.json").read_text(encoding="utf-8"))["events"]
    db = connect(":memory:")
    try:
        for packet, expected in zip(events, ("publishable", "held")):
            filename = tmp_path / (packet["id"] + ".json")
            filename.write_text(json.dumps({"events": [packet]}), encoding="utf-8")
            assert import_file(db, "survey-import", filename)[0]["result"] == expected
            assert report(db)["survey_release"][0]["state"] == expected
        assert audit(db, HANDLERS)["replay_ok"]
    finally:
        db.close()
