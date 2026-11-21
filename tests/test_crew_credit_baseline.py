from pathlib import Path

from shelterledger.__main__ import HANDLERS, import_file
from shelterledger.report import audit, report
from shelterledger.store import connect

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_second_window_changes_claim_with_still_unverified_overlap():
    db = connect(":memory:")
    try:
        outcome = import_file(db, "crew-import", FIXTURES / "crew-window-baseline.json")
        assert [row["result"] for row in outcome] == ["held", "ready"]
        assert report(db)["crew_credit"] == [
            {"team": "utility-crew", "claim": 20, "witnessed": 20, "state": "ready"}
        ]
        assert audit(db, HANDLERS)["replay_ok"]
    finally:
        db.close()
