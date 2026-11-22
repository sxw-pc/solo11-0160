"""Team service credit reconciled from independently held field evidence.

The two work desks can see the same real work event in two short-interval
windows. Sheet minutes are only a report summary: a certificate is issued for
proven simultaneous work, measured in actually contributed person-minutes, and
no per-person allowance is ever signed.

Two evidence routes may certify the same kind of crew work-time certificate:

* route A (badge route): an in-shift issuance anchor for a physical badge plus
  the two-desk co-witnessed forwarding chain that maps desk-local codes onto
  that badge. Distinct local codes without a distinct issuance anchor never
  imply two people, and the route does not require physical isolation of the
  desks.
* route B (station route): mutually exclusive tool stations that hold at most
  one real contributor at a time, with independent double-signed
  simultaneous-occupancy observations. Button receipts alone never establish
  two contributors, and a moved station without independently verified
  simultaneous occupancy certifies nothing extra.

Every actual contributor counts at most one person-minute per minute; two
different people working simultaneously each count one. When the two routes
are both fully proven over a segment and their facts are mutually exclusive,
the conflict and the minimal field witnesses that create it are reported and
the higher number is never taken.
"""

import json

from .store import log, many, one

DEFAULT_SHIFT = "shift-1"
MAX_VOLUNTEERS = 3
MAX_SEGMENTS = 3

BADGE = "badge_anchor"
STATION = "station_witness"
BUTTON = "button_receipt"
BACKUP = "backup_retrieval"


def _j(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class _Nodes:
    """Union-find over (desk, local_code) physical-credential observations."""

    def __init__(self):
        self.parent = {}
        self.badges = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def link(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra
            self.badges.setdefault(ra, set()).update(self.badges.pop(rb, set()))

    def attach_badge(self, x, badge):
        root = self.find(x)
        self.badges.setdefault(root, set()).add(badge)

    def badge_of(self, x):
        if x not in self.parent:
            return None
        badges = self.badges.get(self.find(x), set())
        return next(iter(badges)) if len(badges) == 1 else None

    def conflicts(self):
        out = []
        for root, badges in self.badges.items():
            if len(badges) > 1:
                out.append((root, sorted(badges)))
        return out


def _active_evidence(db, team, shift):
    return [
        e
        for e in many(
            db,
            "SELECT * FROM crew_evidence WHERE team=? AND shift=? ORDER BY id",
            (team, shift),
        )
        if e["state"] != "rejected"
    ]


def _identity_graph(db, team, shift):
    nodes = _Nodes()
    anchors = []
    for e in _active_evidence(db, team, shift):
        if e["kind"] != BADGE:
            continue
        payload = json.loads(e["payload"])
        anchors.append((e, payload))
        badge = payload["badge_id"]
        for link in payload.get("links", []):
            a = (link["desk"], link["local_code"])
            nodes.find(a)
            nodes.attach_badge(a, badge)
            for other in link.get("same_as", []):
                b = (other["desk"], other["local_code"])
                nodes.link(a, b)
                nodes.attach_badge(b, badge)
    return nodes, anchors


def _restart_seen(db, team, shift, rnd, start, stop):
    """A new issuance anchor or a fresh in-period station witness at a round.

    The witness must belong to the same or a later round than the repeated
    window and fall inside the segment; merely having different local paper
    codes with no new anchor is never enough.
    """
    for e in _active_evidence(db, team, shift):
        if e["round"] < rnd:
            continue
        if e["kind"] == BADGE and start <= (e["signed_at"] if e["signed_at"] is not None else -1) < stop:
            return True
        if e["kind"] in (STATION, BACKUP) and e["covers_start"] is not None:
            if e["covers_start"] <= start and stop <= e["covers_stop"]:
                return True
    return False


def _route_a_windows(eligible, seg_windows):
    """Eligible windows grouped by round; excluded rounds simply do not appear."""
    if eligible:
        groups = {}
        for w in eligible:
            groups.setdefault(w["round"], []).append(w)
        return [sorted(v, key=lambda w: w["id"]) for _, v in sorted(groups.items())]
    return [[w] for w in seg_windows]  # keep unknowns visible for the missing list


def _route_a_segment(nodes, segment_windows, anchors):
    """segment_windows is a list of per-round window groups."""
    badges, evidence, unknown, conflicted = [], [], [], []
    for group in segment_windows:
        for w in group:
            key = (w["desk"], w["local_code"])
            if key not in nodes.parent:
                unknown.append({"desk": w["desk"], "local_code": w["local_code"], "window": w["id"]})
                continue
            root = nodes.find(key)
            root_badges = nodes.badges.get(root, set())
            if len(root_badges) > 1:
                conflicted.append({"desk": w["desk"], "local_code": w["local_code"], "window": w["id"], "badges": sorted(root_badges)})
            else:
                badges.append(next(iter(root_badges)))
    if conflicted:
        return {"status": "conflict", "conflicted": conflicted}
    if unknown:
        return {"status": "unproven", "unknown": unknown}
    used = set(badges)
    for e, payload in anchors:
        if payload["badge_id"] in used:
            evidence.append(e["id"])
    # one distinct contributor per witnessed round; repeated rounds with a real
    # restart anchor each contribute their own person-minutes.
    people_per_round = []
    for group in segment_windows:
        people_per_round.append(len({nodes.badge_of((w["desk"], w["local_code"])) for w in group}))
    return {
        "status": "proven",
        "badges": sorted(set(badges)),
        "people": sum(people_per_round),
        "rounds": people_per_round,
        "evidence": sorted(set(evidence)),
    }


def _route_b_segment(db, team, shift, start, stop, length):
    witnesses, stations, insufficient = [], set(), []
    for e in _active_evidence(db, team, shift):
        if e["kind"] not in (STATION, BACKUP) or e["shift"] != shift:
            continue
        if not (
            e["covers_start"] is not None
            and e["covers_start"] <= start
            and stop <= e["covers_stop"]
            and start <= (e["signed_at"] if e["signed_at"] is not None else -1) < stop
        ):
            continue
        payload = json.loads(e["payload"])
        sigs = payload.get("signatures", [])
        if len(set(sigs)) < 2 or not payload.get("simultaneous_occupancy"):
            insufficient.append(e["id"])
            continue
        witnesses.append(e["id"])
        stations.add(payload.get("station"))
    for e in many(
        db,
        "SELECT * FROM crew_evidence WHERE team=? AND shift=? AND kind=? ORDER BY id",
        (team, shift, BUTTON),
    ):
        if e["covers_start"] is not None and e["covers_start"] <= start and stop <= e["covers_stop"]:
            insufficient.append(e["id"])
    if len(stations) >= 2:
        return {
            "status": "proven",
            "people": 2,
            "evidence": sorted(witnesses),
            "stations": sorted(stations),
        }
    return {"status": "unproven", "weak_evidence": sorted(set(insufficient))}


def reconcile(db, team, claim, evidence_id, shift=DEFAULT_SHIFT):
    windows = many(
        db,
        "SELECT * FROM crew_window WHERE team=? AND shift=? ORDER BY start,id",
        (team, shift),
    )
    atoms = sorted({(w["start"], w["stop"]) for w in windows})
    if len(atoms) > MAX_SEGMENTS:
        raise ValueError("scope is limited to three discrete time segments")
    nodes, anchors = _identity_graph(db, team, shift)
    if len({json.loads(e["payload"])["badge_id"] for e, _ in anchors}) > MAX_VOLUNTEERS:
        raise ValueError("scope is limited to three volunteers")

    graph_conflicts = nodes.conflicts()
    segments, conflicts, missing, contradicted = [], [], [], set()
    if graph_conflicts:
        conflict_badges = {badge for _, badges in graph_conflicts for badge in badges}
        contradicted.update(
            e["id"] for e, payload in anchors if payload["badge_id"] in conflict_badges
        )

    for start, stop in atoms:
        length = stop - start
        seg_windows = [w for w in windows if w["start"] == start and w["stop"] == stop]
        excluded = []
        eligible = []
        for w in seg_windows:
            if w["round"] > 1 and not _restart_seen(
                db, team, shift, w["round"], start, stop
            ):
                excluded.append(
                    {
                        "window": w["id"],
                        "reason": "no witnessed restart: the same staffing cannot be measured twice",
                    }
                )
            else:
                eligible.append(w)

        route_a_groups = _route_a_windows(eligible, seg_windows)
        route_a = _route_a_segment(nodes, route_a_groups, anchors)
        route_b = _route_b_segment(db, team, shift, start, stop, length)
        seg = {
            "start": start,
            "stop": stop,
            "length": length,
            "sheets": [w["id"] for w in seg_windows],
            "route_a": route_a,
            "route_b": route_b,
            "excluded": excluded,
        }

        if graph_conflicts or route_a["status"] == "conflict":
            if route_a["status"] == "conflict":
                bad = {badge for c in route_a["conflicted"] for badge in c["badges"]}
                witnesses = sorted(
                    {e["id"] for e, p in anchors if p["badge_id"] in bad}
                )
                contradicted.update(witnesses)
            else:
                witnesses = sorted(contradicted)
            reason = "identity and station relation cannot hold across all time slices"
            conflicts.append({"segment": [start, stop], "reason": reason, "minimal_witnesses": witnesses})
            seg.update(minutes=0, state="conflict")
        else:
            a_ok = route_a["status"] == "proven"
            b_ok = route_b["status"] == "proven"
            a_minutes = route_a["people"] * length if a_ok else None
            b_minutes = route_b["people"] * length if b_ok else None
            if a_ok and b_ok and a_minutes != b_minutes:
                witnesses = sorted(set(route_a["evidence"]) | set(route_b["evidence"]))
                reason = (
                    f"route A proves {a_minutes} and route B proves {b_minutes} "
                    "person-minutes over the same segment; the claims are mutually exclusive"
                )
                conflicts.append({"segment": [start, stop], "reason": reason, "minimal_witnesses": witnesses})
                contradicted.update(witnesses)
                seg.update(minutes=0, state="conflict")
            elif a_ok or b_ok:
                minutes = a_minutes if a_ok else b_minutes
                seg.update(minutes=minutes, state="proven", route="A" if a_ok and not b_ok else "B" if b_ok and not a_ok else "A+B")
            else:
                seg.update(minutes=0, state="unproven")
                if route_a["status"] == "unproven":
                    for u in route_a["unknown"]:
                        missing.append(
                            f"badge issuance anchor and two-desk co-witness forwarding for "
                            f"{u['desk']} local code {u['local_code']} (sheet {u['window']})"
                        )
                if route_b["status"] == "unproven" and route_b["weak_evidence"]:
                    missing.append(
                        "independent double-signed simultaneous occupancy for both exclusive "
                        f"stations; receipts {', '.join(route_b['weak_evidence'])} cannot stand in for two contributors"
                    )
                elif route_b["status"] == "unproven":
                    missing.append(
                        "independent double-signed simultaneous occupancy observations at both "
                        "exclusive tool stations covering the segment"
                    )
            for item in excluded:
                missing.append(f"{item['reason']} (sheet {item['window']})")
        segments.append(seg)

    certified = sum(s["minutes"] for s in segments)
    sheet_minutes = one(
        db,
        "SELECT COALESCE(SUM(minutes),0) AS t FROM crew_window WHERE team=? AND shift=?",
        (team, shift),
    )["t"]

    # Re-derive every evidence state deterministically so later real work
    # orders can visibly overturn evidence that an earlier certificate used.
    # Explicit rejections (e.g. a late witness that cannot be made) are kept.
    weak = {eid for s in segments for eid in (s["route_b"].get("weak_evidence") or [])}
    for e in many(db, "SELECT * FROM crew_evidence WHERE team=? AND shift=? ORDER BY id", (team, shift)):
        if e["state"] == "rejected":
            continue
        if e["id"] in contradicted:
            state, reason = "contradicted", "overturned by a later mutually exclusive field original"
        elif e["kind"] == BUTTON or e["id"] in weak or e["state"] == "insufficient":
            state, reason = "insufficient", "receipt or single signature cannot establish two simultaneous contributors"
        else:
            state, reason = "recorded", "held as an independently witnessed field original"
        db.execute("UPDATE crew_evidence SET state=?, reason=? WHERE id=?", (state, reason, e["id"]))

    if conflicts:
        state = "conflict"
        route = "A/B"
    elif certified > 0:
        state = "certified"
        proven_routes = {s.get("route") for s in segments if s["state"] == "proven"}
        route = "+".join(sorted(r for r in proven_routes if r))
    else:
        state = "held"
        route = "none"
    if certified < claim and not conflicts:
        missing.insert(0, f"{claim - certified} person-minutes of the claimed {claim} remain without a field original")

    identity = {
        f"{desk}:{code}": nodes.badge_of((desk, code))
        for desk, code in sorted(nodes.parent)
    }
    basis = {
        "segments": segments,
        "identity_map": identity,
        "raw_sheet_minutes": sheet_minutes,
        "certified_person_minutes": certified,
    }
    basis_text, missing_text, conflicts_text = _j(basis), _j(missing), _j(conflicts)
    previous = one(
        db,
        "SELECT * FROM crew_certificate WHERE team=? AND shift=? ORDER BY version DESC LIMIT 1",
        (team, shift),
    )
    signature = (int(claim), sheet_minutes, certified, state, route, basis_text, missing_text, conflicts_text)
    if previous and (
        previous["claim"],
        previous["sheet_minutes"],
        previous["certified_minutes"],
        previous["state"],
        previous["route"],
        previous["basis"],
        previous["missing"],
        previous["conflicts"],
    ) == signature:
        # No new field original changed the adjudication: keep the same version.
        db.execute("UPDATE crew_certificate SET event_id=? WHERE id=?", (evidence_id, previous["id"]))
        version = previous["version"]
    else:
        version = (previous["version"] + 1) if previous else 1
        db.execute(
            "INSERT INTO crew_certificate(team,shift,version,claim,sheet_minutes,certified_minutes,state,route,basis,missing,conflicts,event_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (team, shift, version, int(claim), sheet_minutes, certified, state, route, basis_text, missing_text, conflicts_text, evidence_id),
        )
    db.execute(
        "INSERT INTO crew_credit(team,shift,claim,sheet_minutes,certified_minutes,state,route,basis,missing,conflicts,certificate_version) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(team,shift) DO UPDATE SET "
        "claim=excluded.claim,sheet_minutes=excluded.sheet_minutes,certified_minutes=excluded.certified_minutes,"
        "state=excluded.state,route=excluded.route,basis=excluded.basis,missing=excluded.missing,"
        "conflicts=excluded.conflicts,certificate_version=excluded.certificate_version",
        (team, shift, int(claim), sheet_minutes, certified, state, route, basis_text, missing_text, conflicts_text, version),
    )
    log(
        db,
        "crew",
        team,
        state,
        f"certificate v{version}: {certified} proven person-minutes vs {sheet_minutes} raw sheet minutes and claim {int(claim)}",
        evidence_id,
    )
    return state


def certificate(db, team, shift=DEFAULT_SHIFT):
    row = one(db, "SELECT * FROM crew_credit WHERE team=? AND shift=?", (team, shift))
    if not row:
        return None
    out = dict(row)
    for key in ("basis", "missing", "conflicts"):
        out[key] = json.loads(out[key])
    return out


def next_shift_actions(db, team, shift=DEFAULT_SHIFT):
    """What the next shift can actually capture, and what each outcome promises."""
    late_used = one(
        db,
        "SELECT COUNT(*) AS n FROM crew_late_action WHERE team=?", (team,)
    )["n"]
    quota = late_used == 0
    badge_root = bool(
        one(
            db,
            "SELECT 1 FROM crew_evidence WHERE team=? AND kind=? AND shift=?",
            (team, BADGE, shift),
        )
    )
    return [
        {
            "action": "retrieve_independently_signed_backup",
            "available": quota,
            "limit": "one read-back or re-sign only; the original must already have been signed in the work period",
            "outcomes": {
                "backup holds two independent in-period signatures covering both exclusive stations at once":
                    "route B certificate of 20 person-minutes",
                "backup holds a single signature or button receipts only":
                    "stays held; two receipts never establish two contributors",
            },
        },
        {
            "action": "resign_badge_identity",
            "available": quota and badge_root,
            "limit": "identity can be back-filled only while the real badge issuance root still exists",
            "outcomes": {
                "both desk local codes resolve to one physical badge":
                    "route A certificate of 10 person-minutes",
                "two distinct issuance anchors resolve independently":
                    "route A certificate of 20 person-minutes",
                "badge root evidence no longer exists":
                    "identity cannot be restored; only existing exclusive-station originals may be used",
            },
        },
        {
            "action": "fresh_simultaneous_occupancy_witness",
            "available": False,
            "limit": "dual-station simultaneous occupancy had to be witnessed in the original period and cannot be produced afterwards; replays never borrow another shift's or round's badge or station proof",
            "outcomes": {},
        },
    ]
