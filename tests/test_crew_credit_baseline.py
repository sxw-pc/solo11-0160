import json
from pathlib import Path

from shelterledger.__main__ import HANDLERS, import_file
from shelterledger.report import audit, report
from shelterledger.store import connect

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_sheets_alone_stay_held_but_raw_summary_is_retained():
    db = connect(":memory:")
    try:
        outcome = import_file(db, "crew-import", FIXTURES / "crew-window-baseline.json")
        # Two sheets of ten sheet-minutes do not prove whether one or two real
        # contributors worked; local codes on paper are never two people.
        assert [row["result"] for row in outcome] == ["held", "held"]
        credit = report(db)["crew_credit"]
        assert len(credit) == 1
        row = credit[0]
        assert row["team"] == "utility-crew"
        assert row["shift"] == "shift-1"
        assert row["claim"] == 20
        assert row["sheet_minutes"] == 20
        assert row["certified_minutes"] == 0
        assert row["state"] == "held"
        assert row["route"] == "none"
        assert row["conflicts"] == "[]"
        assert row["certificate_version"] == 2  # version grows as raw sheet totals grow
        assert json.loads(row["missing"])  # the missing field originals are named
        assert report(db)["crew_window"][0]["minutes"] == 10
        assert audit(db, HANDLERS)["replay_ok"]
    finally:
        db.close()
