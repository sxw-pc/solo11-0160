"""Capture signed aggregate feedback, never a resident register."""

from . import survey_release
from .store import log


def capture(db, event):
    yes, total = event["yes"], event["respondents"]
    if not isinstance(yes, int) or not isinstance(total, int) or not 0 <= yes <= total:
        raise ValueError("invalid survey count")
    if not event.get("reader"):
        raise ValueError("independent tally reader required")
    observed = "minimum_met" if total >= 3 else "too_few"
    log(db, "survey-tally", event["round_id"], observed, "signed aggregate", event["id"])
    return survey_release.review(db, event["round_id"], observed, event["id"])


def sheet(db, event):
    """Record the original answer sheet: each seat answers one 0 or 1."""
    answers = event.get("answers")
    if not isinstance(answers, dict) or not answers:
        raise ValueError("original answer sheet required")
    if len(answers) > survey_release.MAX_SEATS:
        raise ValueError("at most six seats")
    for seat, bit in answers.items():
        if not isinstance(seat, str) or not seat:
            raise ValueError("seat labels are opaque internal marks")
        if isinstance(bit, bool) or bit not in (0, 1):
            raise ValueError("each seat answers a single 0 or 1")
    return survey_release.record_sheet(db, event["round_id"], answers, event["id"])


def _sites(owner):
    sites = owner.get("sites")
    if not isinstance(sites, list) or not sites or len(set(sites)) != len(sites):
        raise ValueError("sites must be a non-empty list of unique seats")
    if any(not isinstance(s, str) or not s for s in sites):
        raise ValueError("seat labels are opaque internal marks")
    return list(sites)


def _bounds(owner, size):
    kinds = [k for k in ("exact", "at_least", "at_most", "interval") if k in owner]
    if len(kinds) != 1:
        raise ValueError("exactly one bound per notice")
    if "exact" in owner:
        lo = hi = owner["exact"]
    elif "at_least" in owner:
        lo, hi = owner["at_least"], size
    elif "at_most" in owner:
        lo, hi = 0, owner["at_most"]
    else:
        lo, hi = owner["interval"]
    if (
        isinstance(lo, bool)
        or isinstance(hi, bool)
        or not isinstance(lo, int)
        or not isinstance(hi, int)
        or not 0 <= lo <= hi <= size
    ):
        raise ValueError("bounds outside coverage")
    return lo, hi


def _true_sum(answers, sites, what):
    if any(p not in answers for p in sites):
        raise ValueError(what + " covers seats outside the answer sheet")
    return sum(answers[p] for p in sites)


def notice(db, event):
    """Record old paper already posted; it stays a known constraint."""
    sites = _sites(event)
    lo, hi = _bounds(event, len(sites))
    round_id = event["round_id"]
    answers = survey_release.sheet_answers(db, round_id)
    if answers is not None and not lo <= _true_sum(answers, sites, "notice") <= hi:
        raise ValueError("notice contradicts the answer sheet")
    return survey_release.record_notice(db, round_id, event["id"], sites, lo, hi, "posted")


def request(db, event):
    """Review candidate statistics against everything already public."""
    round_id = event["round_id"]
    answers = survey_release.sheet_answers(db, round_id)
    if answers is None:
        raise ValueError("original answer sheet required before a release request")
    candidates = event.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("candidates required")
    prepared = []
    for c in candidates:
        sites = _sites(c)
        reported = c.get("reported")
        if isinstance(reported, bool) or not isinstance(reported, int):
            raise ValueError("reported sum must be an integer")
        if reported != _true_sum(answers, sites, "candidate"):
            raise ValueError("reported sum contradicts the answer sheet")
        prepared.append({"sites": sites, "reported": reported})
    protected = event.get("protected", [])
    if not isinstance(protected, list) or any(p not in answers for p in protected):
        raise ValueError("protected seats must come from the answer sheet")
    triggers = event.get("demand_triggers", [])
    if not isinstance(triggers, list) or len(triggers) > survey_release.MAX_TRIGGERS:
        raise ValueError("at most two registered thresholds")
    if any(isinstance(t, bool) or not isinstance(t, int) or t < 1 for t in triggers):
        raise ValueError("thresholds are positive integers")
    groups = None
    if event.get("groups"):
        groups = []
        seen = set()
        for g in event["groups"]:
            sites = _sites({"sites": g})
            if seen & set(sites):
                raise ValueError("groups must be mutually exclusive in one evidence")
            seen |= set(sites)
            groups.append({"sites": sites, "reported": _true_sum(answers, sites, "group")})
    return survey_release.review_request(db, event["id"], round_id, prepared, protected, triggers, groups)
