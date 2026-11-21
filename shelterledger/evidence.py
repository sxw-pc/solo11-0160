"""Evidence-based per-bed reconstruction.

原始单据（床卡、床级故障、分床/换床、车闸时窗、到点接收、转移计划、签领、
撤证、准入申请）是不可变事实；本模块在每批原件导入后按原始时刻与见证关系
确定性地重建：

* journey         逐人的可行行程集合（离点/到达都是时窗，不是单点时刻）
* bed_interval    逐床的在用 occupied / 待核 unverified / 预留 reserved / 故障 fault 区间
* bed_verdict     目标时刻逐床 free / partial / occupied / unsafe
* admission_decision  甲（实时准入）与乙（跨点预案）两份合同的独立裁定
* reversal        对旧裁定（含旧总量排序结论）的撤回记录

所有时刻在重裁层归一化为整数秒的闭区间；原件层保留原始含/不含边界摘要。
"""
import json
from datetime import datetime
from .store import one, many, log, DERIVED_TABLES


def ts(value):
    """ISO 8601 时刻 -> 整数秒（朴素时间按同一基准换算，只做先后比较）。"""
    return int(datetime.fromisoformat(value).timestamp())


def fmt(sec):
    return datetime.fromtimestamp(sec).isoformat() if sec is not None else None


def norm_window(lo, lo_inc, hi, hi_inc):
    """把任意开闭边界的时窗归一化为整数秒闭区间 [L,H]。"""
    L = ts(lo) + (0 if lo_inc else 1)
    H = ts(hi) - (0 if hi_inc else 1)
    return L, H


def _open_ids(db):
    return {(r['target_kind'], r['target_id']) for r in many(db, 'SELECT target_kind,target_id FROM evidence_revocation')}


def _revoked(rev, kind, eid):
    return (kind, eid) in rev


def rebuild(db, source_command=None, source_sha=None, reason='new evidence batch'):
    """按当前全部有效原件重建所有派生表，并落一版可查报表快照。"""
    rev = _open_ids(db)
    cards = [r for r in many(db, 'SELECT * FROM bed_card WHERE state=? ORDER BY shelter_id,bed_no,id', ('active',))
             if not _revoked(rev, 'bed_card', r['id'])]
    # 已关闭的床故障仍保留故障区间 [at, closed_at]，只是不再影响当前安全床计数
    all_bed_faults = [r for r in many(db, 'SELECT * FROM bed_fault ORDER BY at,id')
                      if not _revoked(rev, 'bed_fault', r['id'])]
    faults = [r for r in all_bed_faults if r['state'] == 'open']
    assigns = [r for r in many(db, 'SELECT * FROM bed_assignment WHERE state IN (?,?) ORDER BY at,id', ('active', 'moved'))
               if not _revoked(rev, 'bed_assignment', r['id'])]
    gates = [r for r in many(db, "SELECT * FROM gate_record WHERE state='open' ORDER BY at_lo,id")
             if not _revoked(rev, 'gate_record', r['id'])]
    arrivals = [r for r in many(db, "SELECT * FROM arrival_record WHERE state='open' ORDER BY at_lo,id")
                if not _revoked(rev, 'arrival_record', r['id'])]
    plans = [r for r in many(db, "SELECT * FROM transfer_plan WHERE state='planned' ORDER BY at,id")
             if not _revoked(rev, 'transfer_plan', r['id'])]
    supplies = [r for r in many(db, "SELECT * FROM supply WHERE state='issued' ORDER BY at,id")
                if not _revoked(rev, 'supply', r['id'])]
    requests = [r for r in many(db, 'SELECT * FROM admission_request ORDER BY at,id')]

    vrow = one(db, 'SELECT COALESCE(MAX(version),0)+1 AS v FROM adjudication_version')
    version = vrow['v'] if vrow else 1
    for t in DERIVED_TABLES:
        db.execute(f'DELETE FROM {t}')

    by_person = {}
    for a in assigns:
        by_person.setdefault((a['incident'], a['person_id']), []).append(a)
    persons = set(by_person) | {(p['incident'], p['person_id']) for p in plans}

    journeys, intervals = [], []
    journey_seq = 0

    # ---- 1. 逐人重建行程链与逐床区间 -------------------------------------
    for key in sorted(persons):
        incident, person = key
        chain = _build_chain(key, by_person.get(key, []), gates, arrivals, plans, supplies, rev)
        for leg in chain['legs']:
            journey_seq += 1
            jkey = f'J{journey_seq:04d}'
            journeys.append(dict(version=version, incident=incident, person_id=person,
                                 origin_shelter=leg['origin'], dest_shelter=leg['dest'],
                                 exit_lo=leg['exit'][0], exit_hi=leg['exit'][1],
                                 arrival_lo=leg['arrival'][0], arrival_hi=leg['arrival'][1],
                                 complete=int(leg['complete']), feasible=int(leg['feasible']),
                                 state=leg['state'], conflict=json.dumps(leg['conflict'], sort_keys=True),
                                 evidence_ids=json.dumps(leg['evidence'], sort_keys=True), key=jkey))
            for iv in leg['intervals']:
                iv['version'] = version
                iv['incident'] = incident
                iv['person_id'] = person
                iv['journey_key'] = jkey
                intervals.append(iv)
        # 无法挂到任何行程腿上的孤立冲突（如全程无分床）才单独成行；
        # 已挂在腿上的冲突随腿留存，不重复生成 journey。
        leg_conflicts = {json.dumps(leg['conflict'], sort_keys=True)
                         for leg in chain['legs'] if leg['conflict'] is not None}
        for c in chain['conflicts']:
            if json.dumps(c, sort_keys=True) in leg_conflicts:
                continue
            journey_seq += 1
            jkey = f'J{journey_seq:04d}'
            journeys.append(dict(version=version, incident=incident, person_id=person,
                                 origin_shelter=c.get('origin'), dest_shelter=c.get('dest'),
                                 exit_lo=None, exit_hi=None, arrival_lo=None, arrival_hi=None,
                                 complete=0, feasible=0, state='conflicted',
                                 conflict=json.dumps(c, sort_keys=True),
                                 evidence_ids=json.dumps(c['evidence_ids'], sort_keys=True), key=jkey))

    # ---- 2. 床级故障区间（含水类故障：关闭该点全部床） --------------------
    water_faults = [r for r in many(db, "SELECT * FROM fault WHERE closed_at IS NULL AND kind='water'")
                    if not _revoked(rev, 'fault', r['id'])]
    for f in all_bed_faults:
        intervals.append(dict(version=version, shelter_id=f['shelter_id'], bed_no=f['bed_no'],
                              kind='fault', lo=ts(f['at']),
                              hi=ts(f['closed_at']) if f['closed_at'] else None,
                              person_id=None, incident=None,
                              evidence_ids=json.dumps([f['id']]), journey_key=None))
    for wf in water_faults:
        for c in cards:
            if c['shelter_id'] != wf['shelter_id']:
                continue
            intervals.append(dict(version=version, shelter_id=wf['shelter_id'], bed_no=c['bed_no'],
                                  kind='fault', lo=ts(wf['at']),
                                  hi=ts(wf['closed_at']) if wf['closed_at'] else None,
                                  person_id=None, incident=None,
                                  evidence_ids=json.dumps([wf['id']]), journey_key=None))

    # ---- 3. 转移计划只形成目的床预留区间，绝不提前释放原床 ----------------
    for p in plans:
        if not p['bed_no']:
            continue
        end = next((ts(a['at']) for a in assigns
                    if a['incident'] == p['incident'] and a['person_id'] == p['person_id']
                    and a['shelter_id'] == p['to_shelter'] and ts(a['at']) >= ts(p['at'])), None)
        intervals.append(dict(version=version, shelter_id=p['to_shelter'], bed_no=p['bed_no'],
                              kind='reserved', lo=ts(p['at']), hi=end, person_id=p['person_id'],
                              incident=p['incident'], evidence_ids=json.dumps([p['id']]), journey_key=None))

    _persist(db, journeys, intervals)

    # ---- 4. 容量/占用：床卡设施按安全床号计数，不再按 beds_lost 推断 ------
    _reconcile_bedcard_shelters(db, version, cards, faults, water_faults, intervals, requests, rev)
    _persist_verdicts(db, version, requests)

    # ---- 5. 床级故障裁定转移，并撤回按总量排序得出的旧结论 ----------------
    _reconcile_intake_states(db, version, assigns, gates, faults, cards, plans, rev)

    db.execute('INSERT INTO adjudication_version(version,source_command,source_sha,reason) VALUES(?,?,?,?)',
               (version, source_command, source_sha, reason))
    from .report import TABLES
    # 快照只装业务表（含本版重裁结果），不装快照表自身，避免自指膨胀
    snap = {t: [dict(x) for x in many(db, f'SELECT * FROM {t} ORDER BY rowid')] for t in TABLES}
    snap['event'] = [dict(x) for x in many(db, 'SELECT id,kind,sha FROM event ORDER BY rowid')]
    snap['source_file'] = [dict(x) for x in many(db, 'SELECT id,command,sha FROM source_file ORDER BY id')]
    for row in snap.get('decision_log', []):
        row.pop('at', None)
    db.execute('INSERT INTO report_snapshot(version,payload) VALUES(?,?)',
               (version, json.dumps(snap, sort_keys=True, ensure_ascii=False)))
    return version


def _build_chain(key, assigns, gates, arrivals, plans, supplies, rev):
    """把一个人的全部分派按点切成 stay 链，并配对车闸/接收时窗做可行性校验。

    同一 stay 的多份车闸原件（现场闸机、独立视频）按时窗交集收紧边界；
    到点接收同理。任一交集为空即给出最小冲突证据，不默默挑一张单据。
    """
    incident, person = key
    # stay 既可以由床卡分床建立，也可以由到点接收原件建立（已到点但尚未分床）
    order = []
    for a in assigns:
        order.append((ts(a['at']), 0, a))
    for r in arrivals:
        order.append((ts(r['at_lo']), 1, r))
    stay_map = {}
    for _, _, rec in sorted(order, key=lambda x: (x[0], x[1], x[2]['id'])):
        shelter = rec['shelter_id']
        stay = stay_map.get(shelter)
        if stay is None:
            stay = dict(shelter=shelter, assignments=[], arrivals=[])
            stay_map[shelter] = stay
        if 'bed_no' in rec.keys():
            stay['assignments'].append(rec)
        else:
            stay['arrivals'].append(rec)
    stays = list(stay_map.values())
    person_gates = [g for g in gates if g['incident'] == incident and g['person_id'] == person]
    person_arrivals = [r for r in arrivals if r['incident'] == incident and r['person_id'] == person]
    person_plans = [p for p in plans if p['incident'] == incident and p['person_id'] == person]
    presence = [(ts(s['at']), s['id'], s['shelter_id']) for s in supplies
                if s['incident'] == incident and s['person_id'] == person]
    conflicts, legs = [], []

    # 找不到归属 stay 的车闸原件本身就是最小冲突证据
    for g in person_gates:
        if g['direction'] != 'out' or g['shelter_id'] not in stay_map:
            conflicts.append(dict(type='gate_without_occupancy', origin=g['shelter_id'],
                                  evidence_ids=sorted([g['id']] + [a['id'] for a in assigns])))

    # 按首个证据时刻排序 stay，使 S1（分床）-> S2（接收）构成有序行程链。
    # “只有接收原件”的 stay 是到点未落地：它作为前一腿的到达证据，不单独成段。
    ordered_stays = sorted(stays, key=lambda s: min([ts(a['at']) for a in s['assignments']]
                                                    + [ts(r['at_lo']) for r in s['arrivals']]))
    # 只为带分床的 stay 生成行程腿；但下一证据 stay（含纯接收 stay）提供到达窗口
    leg_stays = [s for s in ordered_stays if s['assignments']]
    for idx, stay in enumerate(leg_stays):
        shelter = stay['shelter']
        cur_pos = ordered_stays.index(stay)
        next_stay = ordered_stays[cur_pos + 1] if cur_pos + 1 < len(ordered_stays) else None
        ev_ids = [a['id'] for a in stay['assignments']] + [r['id'] for r in stay['arrivals']]
        stay_gates = [g for g in person_gates if g['shelter_id'] == shelter and g['direction'] == 'out']
        # 下一 stay 的接收原件就是本段行程的到点证据
        arrival_recs = list(next_stay['arrivals']) if next_stay else []
        leg = dict(origin=shelter, dest=next_stay['shelter'] if next_stay else None,
                   exit=(None, None), arrival=(None, None), complete=False, feasible=True,
                   state='in_occupancy', conflict=None, evidence=list(ev_ids), intervals=[])
        seq = sorted(stay['assignments'], key=lambda x: (ts(x['at']), x['id']))
        local_presence = [(t, sid) for t, sid, sh in presence
                          if sh == shelter and (not seq or _assigned_at(seq, t))]
        if stay_gates:
            leg['evidence'].extend(g['id'] for g in stay_gates)
            window = None
            for g in stay_gates:  # 多份车闸原件 -> 时窗交集
                L, H = norm_window(g['at_lo'], g['lo_inclusive'], g['at_hi'], g['hi_inclusive'])
                window = (L, H) if window is None else (max(window[0], L), min(window[1], H))
            L, H = window
            link_conflict = None
            for t, sid in local_presence:
                if t >= H:  # 本人在 t 仍在场签领，离点必须晚于 t；(…,H] 已无可行秒
                    link_conflict = dict(type='presence_after_gate_closed', origin=shelter,
                                         evidence_ids=sorted(set(leg['evidence'] + [sid])))
                else:
                    L = max(L, t + 1)  # 本人签领给实际离点时刻加下界
            if L > H:
                link_conflict = dict(type='gate_windows_contradictory', origin=shelter,
                                     evidence_ids=sorted(g['id'] for g in stay_gates))
            leg['exit'] = (L, H)
            # 换床区间：除最后一张床外都在换床见证时刻闭合；最后一张床由车闸窗口收口
            for j, a in enumerate(seq):
                if j + 1 < len(seq):
                    leg['intervals'].append(dict(shelter_id=a['shelter_id'], bed_no=a['bed_no'],
                                                 kind='occupied', lo=ts(a['at']), hi=ts(seq[j + 1]['at']),
                                                 evidence_ids=json.dumps([a['id']])))
            if seq:
                last_a = seq[-1]
                gate_ev = json.dumps([g['id'] for g in stay_gates])
                # 保证占用 -> 待核（部分行程仍占）-> 已证明空，三段互斥不重叠
                leg['intervals'].append(dict(shelter_id=last_a['shelter_id'], bed_no=last_a['bed_no'],
                                             kind='occupied', lo=ts(last_a['at']), hi=L - 1,
                                             evidence_ids=gate_ev))
                leg['intervals'].append(dict(shelter_id=last_a['shelter_id'], bed_no=last_a['bed_no'],
                                             kind='unverified', lo=L, hi=H, evidence_ids=gate_ev))
                # 离点窗口闭合后，所有相容行程都已离床：该床从 H+1 起被证明为空（可再次入住）
                leg['intervals'].append(dict(shelter_id=last_a['shelter_id'], bed_no=last_a['bed_no'],
                                             kind='vacated', lo=H + 1, hi=None, evidence_ids=gate_ev))
            if arrival_recs:
                leg['evidence'].extend(r['id'] for r in arrival_recs)
                aw = None
                for r in arrival_recs:  # 多份接收原件 -> 时窗交集
                    A, B = norm_window(r['at_lo'], r['lo_inclusive'], r['at_hi'], r['hi_inclusive'])
                    aw = (A, B) if aw is None else (max(aw[0], A), min(aw[1], B))
                A, B = aw
                if A > B:
                    link_conflict = dict(type='arrival_windows_contradictory', dest=next_stay['shelter'],
                                         evidence_ids=sorted(r['id'] for r in arrival_recs))
                leg['arrival'] = (A, B)
                if L >= B:  # 离点最晚也无法早于到达上界，两点不存在可行先后
                    link_conflict = dict(type='exit_after_arrival', origin=shelter, dest=next_stay['shelter'],
                                         evidence_ids=sorted([g['id'] for g in stay_gates]
                                                             + [r['id'] for r in arrival_recs]))
                if next_stay['assignments'] and ts(next_stay['assignments'][0]['at']) < A:
                    link_conflict = dict(type='assigned_before_arrival_window', dest=next_stay['shelter'],
                                         evidence_ids=sorted([r['id'] for r in arrival_recs]
                                                             + [next_stay['assignments'][0]['id']]))
                leg['complete'] = True
            if link_conflict:
                leg['feasible'] = False
                leg['state'] = 'conflicted'
                leg['conflict'] = link_conflict
                conflicts.append(link_conflict)
            else:
                leg['state'] = 'complete_verified' if arrival_recs else 'in_transit'
        else:
            # 无车闸：分床区间按原样保留（最后一张床无界，表示仍在用，计划不得提前释放）
            for j, a in enumerate(seq):
                end = ts(seq[j + 1]['at']) if j + 1 < len(seq) else None
                leg['intervals'].append(dict(shelter_id=a['shelter_id'], bed_no=a['bed_no'],
                                             kind='occupied', lo=ts(a['at']), hi=end,
                                             evidence_ids=json.dumps([a['id']])))
            plan = next((p for p in person_plans if p['from_shelter'] == shelter), None)
            if plan:
                leg['evidence'].append(plan['id'])
                leg['state'] = 'planned_not_departed'  # 有计划无车闸：床不得提前释放
            if next_stay is not None and not arrival_recs:
                c = dict(type='arrival_evidence_missing', dest=next_stay['shelter'],
                         evidence_ids=sorted(ev_ids))
                conflicts.append(c)
                leg['feasible'] = False
                leg['state'] = 'conflicted'
                leg['conflict'] = c
        legs.append(leg)
    if not stays and person_plans:
        p = person_plans[0]
        legs.append(dict(origin=p['from_shelter'], dest=p['to_shelter'], exit=(None, None),
                         arrival=(None, None), complete=False, feasible=True,
                         state='planned_not_departed', conflict=None, evidence=[p['id']], intervals=[]))
    return dict(legs=legs, conflicts=_minimal_conflicts(conflicts))


def _assigned_at(seq, t):
    return any(ts(a['at']) <= t for a in seq)


def _minimal_conflicts(conflicts):
    """同一行程无法闭合时，只保留证据 id 数最少的冲突（最小冲突证据）。"""
    if not conflicts:
        return []
    best = min(len(c['evidence_ids']) for c in conflicts)
    out = [c for c in conflicts if len(c['evidence_ids']) == best]
    return sorted(out, key=lambda c: (c['evidence_ids'], c['type']))


def _persist(db, journeys, intervals):
    for j in journeys:
        db.execute('INSERT INTO journey(version,incident,person_id,origin_shelter,dest_shelter,exit_lo,exit_hi,arrival_lo,arrival_hi,complete,feasible,state,conflict,evidence_ids) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (j['version'], j['incident'], j['person_id'], j['origin_shelter'], j['dest_shelter'], j['exit_lo'],
                    j['exit_hi'], j['arrival_lo'], j['arrival_hi'], j['complete'], j['feasible'],
                    j['state'], j['conflict'], j['evidence_ids']))
    for iv in sorted(intervals, key=lambda x: (x['shelter_id'], str(x['bed_no']), x['kind'], x['lo'] or 0)):
        db.execute('INSERT INTO bed_interval(version,shelter_id,bed_no,kind,lo,hi,person_id,incident,evidence_ids,journey_key) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (iv['version'], iv['shelter_id'], iv['bed_no'], iv['kind'], iv['lo'], iv['hi'],
                    iv['person_id'], iv['incident'], iv['evidence_ids'], iv['journey_key']))


def _cover(iv, t):
    return (iv['lo'] is None or iv['lo'] <= t) and (iv['hi'] is None or t <= iv['hi'])


def _verdict_for(intervals, shelter, bed_no, t):
    """目标时刻逐床判定：unsafe / occupied / partial / free，并回传支撑证据。

    occupied  = 所有相容行程都占用；unverified = 仅部分行程空（有些已离、有些仍占）；
    vacated   = 所有相容行程都已离床，床被证明为空；reserved 是计划预留，不影响空床判定。
    """
    hit = [iv for iv in intervals if iv['shelter_id'] == shelter and iv['bed_no'] == bed_no and _cover(iv, t)]
    evidence = sorted({eid for iv in hit for eid in json.loads(iv['evidence_ids'])})
    if any(iv['kind'] == 'fault' for iv in hit):
        return 'unsafe', evidence
    occ = [iv for iv in hit if iv['kind'] == 'occupied']
    unv = [iv for iv in hit if iv['kind'] == 'unverified']
    if not occ and not unv:
        return 'free', evidence
    if occ and not unv:
        return 'occupied', evidence
    return 'partial', evidence  # 仅部分相容行程空：有些行程已离床，有些仍占用


def _reconcile_bedcard_shelters(db, version, cards, faults, water_faults, intervals, requests, rev):
    faulted = {(f['shelter_id'], f['bed_no']) for f in faults}
    water_closed = {w['shelter_id'] for w in water_faults}
    shelters = sorted({c['shelter_id'] for c in cards})
    for s in shelters:
        scards = [c for c in cards if c['shelter_id'] == s]
        safe = [c for c in scards if (s, c['bed_no']) not in faulted and s not in water_closed]
        beds = len(safe)
        accessible = sum(1 for c in safe if c['accessible'])
        water_ok = s not in water_closed
        state = 'closed' if not water_ok or beds == 0 else 'limited' if beds < len(scards) else 'open'
        reason = 'per-bed faults and witness evidence' if state != 'open' else 'all bed cards verified safe'
        old = one(db, 'SELECT * FROM capacity WHERE shelter_id=? ORDER BY id DESC LIMIT 1', (s,))
        # 床号重裁是床卡设施的权威容量；即便本批还有 beds_lost 总量账，也以床号结果收尾
        if not old or (old['beds'], old['accessible'], old['state'], old['reason']) != (beds, accessible, state, reason):
            db.execute('INSERT INTO capacity(shelter_id,beds,accessible,water,state,reason) VALUES(?,?,?,?,?,?)',
                       (s, beds, accessible, int(water_ok), state, reason))
            log(db, 'evidence', s, state, f'bed-card adjudication v{version}: {reason}')
        present = len({iv['person_id'] for iv in intervals
                       if iv['shelter_id'] == s and iv['kind'] == 'occupied' and iv['hi'] is None})
        remaining = max(0, beds - present)
        usage_state = 'full' if remaining == 0 else 'available'
        oldu = one(db, 'SELECT * FROM capacity_usage WHERE shelter_id=? ORDER BY id DESC LIMIT 1', (s,))
        if not oldu or (oldu['remaining_beds'], oldu['state']) != (remaining, usage_state):
            db.execute('INSERT INTO capacity_usage(shelter_id,remaining_beds,remaining_accessible,state,reason) VALUES(?,?,?,?,?)',
                       (s, remaining, accessible, usage_state, 'per-bed occupied intervals at latest evidence time'))


def _persist_verdicts(db, version, requests):
    intervals = [dict(r) for r in many(db, 'SELECT * FROM bed_interval WHERE version=?', (version,))]
    cards = many(db, 'SELECT * FROM bed_card WHERE state=?', ('active',))
    for req in requests:
        t = ts(req['at'])
        scards = [c for c in cards if c['shelter_id'] == req['shelter_id']
                  and (not req['needs_accessible'] or c['accessible'])]
        for c in sorted(scards, key=lambda x: x['bed_no']):
            verdict, evidence = _verdict_for(intervals, req['shelter_id'], c['bed_no'], t)
            db.execute('INSERT INTO bed_verdict(version,request_id,shelter_id,bed_no,at,verdict,evidence_ids) VALUES(?,?,?,?,?,?,?)',
                       (version, req['id'], req['shelter_id'], c['bed_no'], t, verdict,
                        json.dumps(evidence, sort_keys=True)))
            reserved = [iv for iv in intervals if iv['shelter_id'] == req['shelter_id']
                        and iv['bed_no'] == c['bed_no'] and iv['kind'] == 'reserved' and _cover(iv, t)]
            rev_ev = sorted({eid for iv in reserved for eid in json.loads(iv['evidence_ids'])})
            conditions = json.dumps({'reserved_by': [iv['person_id'] for iv in reserved],
                                     'reservation_evidence': rev_ev}, sort_keys=True)
            # 合同甲：所有相容行程都空且安全才签实际入场，否则一律待核
            if verdict == 'free':
                a_state, a_reason = 'admitted', 'all compatible journeys show the bed empty and safe'
            elif verdict == 'unsafe':
                a_state, a_reason = 'rejected', 'bed under open fault at target time'
            else:
                a_state, a_reason = 'held_pending_verification', 'some compatible journey still occupies the bed'
            db.execute('INSERT INTO admission_decision(version,request_id,contract,shelter_id,bed_no,at,state,reason,evidence_ids,conditions) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (version, req['id'], 'A-realtime', req['shelter_id'], c['bed_no'], t,
                        a_state, a_reason, json.dumps(evidence, sort_keys=True), conditions))
            # 合同乙：存在一条相容行程使该床在目标时刻空，即可列为有条件候选；候选≠已收容
            if verdict == 'unsafe':
                b_state, b_reason = 'rejected', 'no compatible journey: bed unsafe at target time'
            elif verdict in ('free', 'partial'):
                b_state, b_reason = 'conditional_candidate', 'at least one compatible journey leaves the bed empty'
            else:
                b_state, b_reason = 'rejected', 'every compatible journey keeps the bed occupied'
            db.execute('INSERT INTO admission_decision(version,request_id,contract,shelter_id,bed_no,at,state,reason,evidence_ids,conditions) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (version, req['id'], 'B-contingency', req['shelter_id'], c['bed_no'], t,
                        b_state, b_reason, json.dumps(evidence, sort_keys=True), conditions))


def _reconcile_intake_states(db, version, assigns, gates, faults, cards, plans, rev):
    """床卡设施：只转移实际落在故障床号上的人；撤回按总量/排序得出的旧结论。"""
    faulted = {(f['shelter_id'], f['bed_no']) for f in faults}
    card_shelters = {c['shelter_id'] for c in cards}
    # 收到过 beds_lost 但没有床号的总量故障：旧逻辑会按 intake.at,id 排序挤出较晚入场者
    aggregate_shelters = {f['shelter_id'] for f in many(db, "SELECT * FROM fault WHERE closed_at IS NULL AND kind!='water'")
                          if not _revoked(rev, 'fault', f['id'])}
    latest_bed = {}
    for a in assigns:
        latest_bed[(a['incident'], a['person_id'])] = a
    # 复刻旧排序，逐人说明旧结论为何撤回
    for s in sorted(card_shelters & aggregate_shelters):
        cap = one(db, 'SELECT * FROM capacity WHERE shelter_id=? ORDER BY id DESC LIMIT 1', (s,))
        ordered = [r for r in many(db, "SELECT * FROM intake WHERE shelter_id=? AND state IN ('admitted','needs_transfer') ORDER BY at,id", (s,))]
        for pos, i in enumerate(ordered):
            a = latest_bed.get((i['incident'], i['person_id']))
            bed_safe = a is not None and (s, a['bed_no']) not in faulted
            if pos >= (cap['beds'] if cap else 0) and bed_safe:
                db.execute('INSERT INTO reversal(version,object_type,object_id,old_state,new_state,reason,evidence_ids) VALUES(?,?,?,?,?,?,?)',
                           (version, 'intake', i['id'], i['state'], 'admitted',
                            'withdrawn: aggregate beds_lost ordering would select this person, but bed cards prove their bed is safe',
                            json.dumps([a['id']], sort_keys=True)))
                log(db, 'evidence', i['person_id'], 'ordering_conclusion_withdrawn',
                    'beds_lost cannot identify the faulted bed number')
    for key, a in latest_bed.items():
        if a['shelter_id'] not in card_shelters:
            continue
        incident, person = key
        intake = one(db, 'SELECT * FROM intake WHERE person_id=? AND incident=? ORDER BY at DESC,id DESC LIMIT 1',
                     (person, incident))
        if not intake:
            continue
        on_fault = (a['shelter_id'], a['bed_no']) in faulted
        if on_fault and intake['state'] != 'needs_transfer':
            plan = next((p for p in plans if p['incident'] == incident and p['person_id'] == person
                         and p['from_shelter'] == a['shelter_id']), None)
            target = plan['to_shelter'] if plan else None
            db.execute("UPDATE intake SET state='needs_transfer',reason=? WHERE id=?",
                       (f"bed {a['bed_no']} closed by bed-level fault evidence", intake['id']))
            db.execute('INSERT INTO transfer(person_id,incident,from_shelter,to_shelter,state,reason,basis) VALUES(?,?,?,?,?,?,?)',
                       (person, incident, a['shelter_id'], target, 'planned' if target else 'unplaced',
                        'occupant physically on faulted bed number', 'bed_fault_evidence'))
            db.execute('INSERT INTO reversal(version,object_type,object_id,old_state,new_state,reason,evidence_ids) VALUES(?,?,?,?,?,?,?)',
                       (version, 'intake', intake['id'], intake['state'], 'needs_transfer',
                        f"bed card proves person is on faulted {a['bed_no']}; aggregate ordering withdrawn",
                        json.dumps([a['id']], sort_keys=True)))
            log(db, 'evidence', person, 'needs_transfer', 'bed-level fault adjudicated transfer')
        elif not on_fault and intake['state'] == 'needs_transfer':
            old_reason = intake['reason']
            db.execute("UPDATE intake SET state='admitted',reason=? WHERE id=?",
                       ('restored: bed card proves current bed is safe', intake['id']))
            db.execute('INSERT INTO reversal(version,object_type,object_id,old_state,new_state,reason,evidence_ids) VALUES(?,?,?,?,?,?,?)',
                       (version, 'intake', intake['id'], 'needs_transfer', 'admitted',
                        f'withdrawn: {old_reason}; bed-level evidence supersedes capacity-total inference',
                        json.dumps([a['id']], sort_keys=True)))
            log(db, 'evidence', person, 'admitted', 'aggregate-order transfer conclusion withdrawn')


def current_bed_status(db, shelter_id, bed_no, at):
    """供 CLI/测试直接查询：返回最新版本下某床某时刻的裁定。"""
    v = one(db, 'SELECT MAX(version) AS v FROM adjudication_version')
    if not v:
        return None
    row = one(db, 'SELECT * FROM bed_verdict WHERE version=? AND shelter_id=? AND bed_no=? AND at=? ORDER BY id LIMIT 1',
              (v['v'], shelter_id, bed_no, ts(at)))
    return dict(row) if row else None


def snapshot_at(db, version):
    """读取某一重裁版本的完整报表快照（旧报表版本可查）。"""
    import json as _json
    row = one(db, 'SELECT payload FROM report_snapshot WHERE version=?', (version,))
    return _json.loads(row['payload']) if row else None


def historical_verdict(db, version, request_id, bed_no):
    """从指定版本快照读取逐床裁定（派生表只保留最新版，历史在快照里）。"""
    snap = snapshot_at(db, version)
    if not snap:
        return None
    for r in snap['bed_verdict']:
        if r['request_id'] == request_id and r['bed_no'] == bed_no:
            return r
    return None


def historical_decisions(db, version, request_id, bed_no=None):
    """从指定版本快照读取甲乙两份合同的准入裁定。"""
    snap = snapshot_at(db, version)
    if not snap:
        return []
    return [r for r in snap['admission_decision']
            if r['request_id'] == request_id and (bed_no is None or r['bed_no'] == bed_no)]
