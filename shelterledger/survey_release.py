"""Review queue for public mock feedback summaries.

The desk only publishes aggregate counts, and every count that has been
posted is kept as a constraint the reader is assumed to know.  A new
statistic is released only when each protected seat still has at least
two feasible answers under *all* of those constraints combined; a
statistic that cannot be given exactly falls back to the tightest safe
interval or to a true/false answer against a registered threshold.
History that already pinned an answer is marked as unrecoverable, never
denied, and old numbers are never silently dropped.
"""

import json
from itertools import product

from .store import log, many, one

MAX_SEATS = 6
MAX_NOTICES = 3
MAX_TRIGGERS = 2


def setup(db):
    db.execute(
        """CREATE TABLE IF NOT EXISTS survey_release(
            round_id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            tally_id TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS survey_notice(
            notice_id TEXT PRIMARY KEY,
            round_id TEXT NOT NULL,
            sites TEXT NOT NULL,
            lo INTEGER NOT NULL,
            hi INTEGER NOT NULL,
            provenance TEXT NOT NULL,
            state TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS survey_sheet(
            round_id TEXT PRIMARY KEY,
            answers TEXT NOT NULL
        )"""
    )


def review(db, round_id, observed, tally_id):
    setup(db)
    state = "publishable" if observed == "minimum_met" else "held"
    db.execute(
        """INSERT INTO survey_release VALUES (?,?,?)
           ON CONFLICT(round_id) DO UPDATE SET
              state=excluded.state, tally_id=excluded.tally_id""",
        (round_id, state, tally_id),
    )
    log(db, "survey-release", round_id, state, "minimum source tally", tally_id)
    return state


# --- committed public constraints -------------------------------------


def record_sheet(db, round_id, answers, event_id):
    setup(db)
    blob = json.dumps(answers, sort_keys=True)
    old = one(db, "SELECT answers FROM survey_sheet WHERE round_id=?", (round_id,))
    if old and old["answers"] != blob:
        raise ValueError("answer sheet version conflict")
    if not old:
        db.execute("INSERT INTO survey_sheet VALUES (?,?)", (round_id, blob))
    log(db, "survey-sheet", round_id, "recorded", "original answer sheet", event_id)
    return "recorded"


def sheet_answers(db, round_id):
    setup(db)
    row = one(db, "SELECT answers FROM survey_sheet WHERE round_id=?", (round_id,))
    return json.loads(row["answers"]) if row else None


def _notice_count(db, round_id):
    return one(db, "SELECT COUNT(*) AS c FROM survey_notice WHERE round_id=?", (round_id,))["c"]


def record_notice(db, round_id, notice_id, sites, lo, hi, provenance):
    setup(db)
    if one(db, "SELECT notice_id FROM survey_notice WHERE notice_id=?", (notice_id,)):
        raise ValueError("duplicate notice id")
    if _notice_count(db, round_id) >= MAX_NOTICES:
        raise ValueError("notice budget exhausted for round")
    db.execute(
        "INSERT INTO survey_notice VALUES (?,?,?,?,?,?,?)",
        (notice_id, round_id, json.dumps(sorted(sites)), lo, hi, provenance, provenance),
    )
    log(db, "survey-notice", notice_id, provenance, "public sum in [%d,%d]" % (lo, hi))
    return provenance


def committed(db):
    """Every sum the reader is assumed to know, oldest first."""
    setup(db)
    return [
        (frozenset(json.loads(row["sites"])), row["lo"], row["hi"])
        for row in many(db, "SELECT sites,lo,hi FROM survey_notice ORDER BY rowid")
    ]


def _committed_rows(db):
    return many(db, "SELECT sites,lo,hi,provenance FROM survey_notice ORDER BY rowid")


# --- feasible-world arithmetic ----------------------------------------


def feasible_worlds(positions, constraints):
    """All 0/1 answer sheets consistent with the public constraints."""
    worlds = []
    for bits in product((0, 1), repeat=len(positions)):
        assign = dict(zip(positions, bits))
        if all(lo <= sum(assign[p] for p in sites) <= hi for sites, lo, hi in constraints):
            worlds.append(assign)
    return worlds


def forced_positions(positions, constraints, protected):
    """Protected seats whose answer is the same in every feasible world."""
    worlds = feasible_worlds(positions, constraints)
    if not worlds:
        return set(protected)
    return {p for p in protected if len({w[p] for w in worlds}) == 1}


def newly_forced(positions, base, extra, protected):
    """Protected seats that ``extra`` would pin down beyond ``base``."""
    return forced_positions(positions, list(base) + list(extra), protected) - forced_positions(
        positions, base, protected
    )


def tightest_interval(positions, base, sites, k, protected):
    """Tightest [lo, hi] containing the true sum k that pins nothing new.

    Wider intervals are tried only after every narrower one fails; among
    equal widths the higher lower bound wins, so demand-style "at least"
    statements are preferred over caps.  The trivial [0, len(sites)]
    always terminates the search and adds no information.
    """
    size = len(sites)
    for width in range(size + 1):
        lows = range(max(0, k - width), min(k, size - width) + 1)
        for lo in sorted(lows, reverse=True):
            hi = lo + width
            if not newly_forced(positions, base, [(sites, lo, hi)], protected):
                return lo, hi
    return 0, size


def _implied(positions, constraints, cons):
    sites, lo, hi = cons
    return all(lo <= sum(w[p] for p in sites) <= hi for w in feasible_worlds(positions, constraints))


# --- assessment ---------------------------------------------------------


def assess(positions, base, candidates, protected, triggers=(), groups=None):
    """Pure joint review of candidate statistics against public constraints.

    ``base`` is every sum already public; each candidate is
    {"sites": [...], "reported": k}; ``groups`` is an optional mutually
    exclusive grouping offered for the exact-sum procedure.  The reply
    never names seats: coverages are sizes, not lists.
    """
    positions = sorted(positions)
    protected = sorted(protected)
    base = list(base)
    compromised = sorted(forced_positions(positions, base, protected))
    exact = [(frozenset(c["sites"]), c["reported"], c["reported"]) for c in candidates]
    individual = [not newly_forced(positions, base, [cons], protected) for cons in exact]
    joint_exact_safe = not newly_forced(positions, base, exact, protected)
    acc = list(base)
    out = []
    for cons, alone in zip(exact, individual):
        sites, k, _ = cons
        if not newly_forced(positions, acc, [cons], protected):
            form, exact_ok = {"kind": "exact", "value": k}, True
            acc.append(cons)
        else:
            exact_ok = False
            lo, hi = tightest_interval(positions, acc, sites, k, protected)
            if (lo, hi) == (0, len(sites)):
                form = {"kind": "held"}
            else:
                form = {"kind": "interval", "lo": lo, "hi": hi}
                acc.append((sites, lo, hi))
        hints = []
        for t in triggers:
            if k >= t:
                hint_cons, answer = (sites, t, len(sites)), "at_least"
            else:
                hint_cons, answer = (sites, 0, t - 1), "below"
            safe = not newly_forced(positions, acc, [hint_cons], protected)
            if safe:
                acc.append(hint_cons)
            hints.append(
                {
                    "trigger_at": t,
                    "answer": answer,
                    "publishable": safe,
                    "bounds": [hint_cons[1], hint_cons[2]],
                }
            )
        out.append(
            {
                "coverage": len(sites),
                "individual_exact_safe": alone,
                "exact": {"publishable": exact_ok},
                "release": form,
                "demand": hints,
            }
        )
    released = [r for r in out if r["release"]["kind"] != "held"]
    result = {
        "state": "released" if len(released) == len(out) else "held" if not released else "partial",
        "compromised": compromised,
        "joint_exact_safe": joint_exact_safe,
        "candidates": out,
        "groups": None,
    }
    if groups:
        result["groups"] = _assess_groups(positions, base, groups, protected, candidates)
    return result


def _assess_groups(positions, base, groups, protected, candidates):
    """Procedure one: exact sums of a fixed mutually exclusive grouping.

    Only the fixed groups may be reported; overlapping old tables are
    never subtracted to split a group.  The cross-group coverage is
    derivable only as an interval, never as an exact value.
    """
    cons = [(frozenset(g["sites"]), g["reported"], g["reported"]) for g in groups]
    safe = not newly_forced(positions, base, cons, protected)
    out = {
        "publishable": safe,
        "sizes": [len(g["sites"]) for g in groups],
        "sums": [g["reported"] for g in groups] if safe else None,
        "coverage_interval": None,
        "committed": False,
    }
    if safe and candidates:
        target = frozenset(candidates[0]["sites"])
        values = {sum(w[p] for p in target) for w in feasible_worlds(positions, base + cons)}
        out["coverage_interval"] = [min(values), max(values)]
    return out


def review_request(db, event_id, round_id, candidates, protected, triggers, groups):
    """Assess a release request, commit what is safe, and report promises."""
    setup(db)
    base = committed(db)
    positions = set(protected)
    for sites, _, _ in base:
        positions |= set(sites)
    for c in candidates:
        positions |= set(c["sites"])
    for g in groups or []:
        positions |= set(g["sites"])
    if len(positions) > MAX_SEATS:
        raise ValueError("at most six seats")
    positions = sorted(positions)
    result = assess(positions, base, candidates, protected, triggers, groups)
    committed_now = list(base)
    for index, (cand, r) in enumerate(zip(candidates, result["candidates"])):
        sites = frozenset(cand["sites"])
        form = r["release"]
        if form["kind"] != "held":
            lo, hi = (form["value"], form["value"]) if form["kind"] == "exact" else (form["lo"], form["hi"])
            cons = (sites, lo, hi)
            if cons in committed_now:
                pass  # identical constraint is already public
            elif _notice_count(db, round_id) >= MAX_NOTICES:
                r["release"] = {"kind": "held"}
                log(db, "survey-request", "%s:%d" % (event_id, index), "held", "notice budget exhausted", event_id)
            else:
                record_notice(db, round_id, "%s:%d" % (event_id, index), cand["sites"], lo, hi, "released")
                committed_now.append(cons)
        for hint in r["demand"]:
            if not hint["publishable"]:
                continue
            cons = (sites, hint["bounds"][0], hint["bounds"][1])
            if _implied(positions, committed_now, cons):
                continue  # already entailed by what is public
            if _notice_count(db, round_id) >= MAX_NOTICES:
                hint["publishable"] = False
                hint["reason"] = "notice budget exhausted"
                continue
            record_notice(
                db, round_id, "%s:%d:t%d" % (event_id, index, hint["trigger_at"]),
                cand["sites"], cons[1], cons[2], "released",
            )
            committed_now.append(cons)
    if result["groups"] and result["groups"]["publishable"]:
        ok = True
        for i, g in enumerate(groups):
            cons = (frozenset(g["sites"]), g["reported"], g["reported"])
            if cons in committed_now:
                continue
            if _notice_count(db, round_id) >= MAX_NOTICES:
                ok = False
                break
            record_notice(db, round_id, "%s:g%d" % (event_id, i), g["sites"], g["reported"], g["reported"], "released")
            committed_now.append(cons)
        result["groups"]["committed"] = ok
    released = [r for r in result["candidates"] if r["release"]["kind"] != "held"]
    result["state"] = (
        "released" if len(released) == len(result["candidates"]) else "held" if not released else "partial"
    )
    db.execute(
        """INSERT INTO survey_release VALUES (?,?,?)
           ON CONFLICT(round_id) DO UPDATE SET
              state=excluded.state, tally_id=excluded.tally_id""",
        (round_id, result["state"], event_id),
    )
    # the released combination is verified jointly, never only per item
    result["joint_release_safe"] = not newly_forced(
        positions, base, [c for c in committed_now if c not in base], protected
    )
    log(db, "survey-request", round_id, result["state"], json.dumps(result["candidates"], sort_keys=True), event_id)
    result["promises"] = [
        {
            "coverage": len(json.loads(row["sites"])),
            "lo": row["lo"],
            "hi": row["hi"],
            "provenance": row["provenance"],
            # commitments are never retracted; later overlapping requests
            # are gated against them, so each stays usable
            "continuable": True,
        }
        for row in _committed_rows(db)
    ]
    return result
