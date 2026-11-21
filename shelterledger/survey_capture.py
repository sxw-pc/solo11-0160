"""Capture signed aggregate feedback, never a resident register."""

from .store import log
from .survey_release import review


def capture(db, event):
    yes, total = event["yes"], event["respondents"]
    if not isinstance(yes, int) or not isinstance(total, int) or not 0 <= yes <= total:
        raise ValueError("invalid survey count")
    if not event.get("reader"):
        raise ValueError("independent tally reader required")
    observed = "minimum_met" if total >= 3 else "too_few"
    log(db, "survey-tally", event["round_id"], observed, "signed aggregate", event["id"])
    return review(db, event["round_id"], observed, event["id"])
