import json
from pathlib import Path

from shelterledger.__main__ import HANDLERS, import_file
from shelterledger.report import audit, report
from shelterledger.store import connect


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_carrier_reader_drives_real_sleeve_assignment(tmp_path):
    db = connect(":memory:")
    try:
        import_file(db, "shelter-import", FIXTURES / "shelter-ok.json")
        loaded = json.loads((FIXTURES / "carrier-read-ok.json").read_text(encoding="utf-8"))
        for packet, expected in zip(loaded["events"], ("ready", "needs_capture")):
            filename = tmp_path / (packet["id"] + ".json")
            filename.write_text(json.dumps({"events": [packet]}), encoding="utf-8")
            assert import_file(db, "carrier-import", filename)[0]["result"] == expected
            assert report(db)["carrier_sleeve"][0]["state"] == expected
        assert audit(db, HANDLERS)["replay_ok"]
    finally:
        db.close()
