"""逐秒可核的证据台验收：离线床卡、行程重建、甲乙两份合同、撤证与配给跨点保留。"""
import hashlib
import unittest
from pathlib import Path
from shelterledger.store import connect
from shelterledger.__main__ import import_file, HANDLERS
from shelterledger.evidence import (ts, fmt, snapshot_at, historical_verdict,
                                     historical_decisions)
from shelterledger.report import audit

ROOT = Path(__file__).resolve().parents[1] / 'fixtures' / 'evidence'
MAIN_SEQUENCE = [
    ('shelter-import', '01-beds.json'), ('activation-import', '02-activation.json'),
    ('intake-import', '03-intake.json'), ('shelter-import', '04-fault-and-move.json'),
    ('intake-import', '05-plan-gate-supply-arrival.json'), ('intake-import', '06-contract-requests.json'),
    ('intake-import', '07-video-tightening.json'), ('intake-import', '08-arrival-revocation.json'),
    ('shelter-import', '09-bed-reopen.json'),
    ('shelter-import', '10-four-bed-beds.json'), ('activation-import', '11-counterexample-activation.json'),
    ('intake-import', '12-counterexample-intake.json'), ('shelter-import', '13-counterexample-faults.json'),
]


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.db = connect(':memory:')
        self.version = {}
        for cmd, name in MAIN_SEQUENCE:
            import_file(self.db, cmd, ROOT / name)
            sha = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            self.version[name] = self.db.execute(
                'SELECT version FROM adjudication_version WHERE source_sha=? ORDER BY version DESC LIMIT 1',
                (sha,)).fetchone()['version']
        self.v_plan = self.version['05-plan-gate-supply-arrival.json']
        self.v_req = self.version['06-contract-requests.json']
        self.v_video = self.version['07-video-tightening.json']
        self.v_revoke = self.version['08-arrival-revocation.json']
        self.v_reopen = self.version['09-bed-reopen.json']
        self.v_counter = self.version['13-counterexample-faults.json']

    def tearDown(self):
        self.db.close()

    def test_two_contracts_differ_before_evidence_tightening(self):
        # 10:15 存在 10:13 与 10:17 两条相容离点行程：b3 仅部分行程空
        self.assertEqual(historical_verdict(self.db, self.v_req, 'REQ-A-1015', 'b3')['verdict'], 'partial')
        da = {d['contract']: d['state'] for d in historical_decisions(self.db, self.v_req, 'REQ-A-1015', 'b3')}
        db_ = {d['contract']: d['state'] for d in historical_decisions(self.db, self.v_req, 'REQ-B-1015', 'b3')}
        # 方案甲：不是所有行程都空 -> 明确待核，不得签实际入场
        self.assertEqual(da['A-realtime'], 'held_pending_verification')
        # 方案乙：存在一条空床行程 -> 有条件候选，但候选不等于已收容
        self.assertEqual(db_['B-contingency'], 'conditional_candidate')
        # 现场实时准入不能照抄预案候选
        self.assertNotEqual(da['A-realtime'], db_['B-contingency'])
        # 故障床 unsafe、B 始终占用的 b2 occupied
        self.assertEqual(historical_verdict(self.db, self.v_req, 'REQ-A-1015', 'b1')['verdict'], 'unsafe')
        self.assertEqual(historical_verdict(self.db, self.v_req, 'REQ-A-1015', 'b2')['verdict'], 'occupied')

    def test_contract_decisions_record_contract_number_and_conditions(self):
        rows = historical_decisions(self.db, self.v_req, 'REQ-A-1015', 'b3')
        for r in rows:
            self.assertIn(r['contract'], ('A-realtime', 'B-contingency'))
            self.assertTrue(r['evidence_ids'])

    def test_video_tightening_makes_b3_guaranteed_empty(self):
        # 独立视频把离点上界收紧到 10:14，10:15 已在所有离点窗口之外
        self.assertEqual(historical_verdict(self.db, self.v_video, 'REQ-A-1015', 'b3')['verdict'], 'free')
        da = {d['contract']: d['state'] for d in historical_decisions(self.db, self.v_video, 'REQ-A-1015', 'b3')}
        self.assertEqual(da['A-realtime'], 'admitted')

    def test_transfer_plan_does_not_release_bed_early(self):
        journeys = [j for j in snapshot_at(self.db, self.v_plan)['journey'] if j['person_id'] == 'A']
        # 计划指向 S2，但车闸只给离点时窗；b3 在窗口闭合前不被证明为空
        self.assertTrue(any(j['state'] in ('in_transit', 'complete_verified') for j in journeys))
        intervals = snapshot_at(self.db, self.v_req)['bed_interval']
        b3 = [i for i in intervals if i['shelter_id'] == 'S1' and i['bed_no'] == 'b3']
        self.assertTrue(any(i['kind'] == 'unverified' and i['lo'] <= ts('2026-04-10T10:15:00') <= i['hi']
                            for i in b3))

    def test_complete_journey_built_from_windows(self):
        journeys = [j for j in snapshot_at(self.db, self.v_plan)['journey'] if j['person_id'] == 'A']
        leg = next(j for j in journeys if j['dest_shelter'] == 'S2')
        self.assertEqual(leg['complete'], 1)
        self.assertEqual(leg['feasible'], 1)
        # 车闸 (10:08,10:18] 经 10:12 本人签领收紧下界到 10:12:01
        self.assertEqual(fmt(leg['exit_lo']), '2026-04-10T10:12:01')
        self.assertEqual(fmt(leg['exit_hi']), '2026-04-10T10:18:00')
        self.assertEqual(fmt(leg['arrival_lo']), '2026-04-10T10:20:00')
        self.assertEqual(fmt(leg['arrival_hi']), '2026-04-10T10:30:00')

    def test_arrival_revocation_returns_journey_to_in_transit(self):
        journeys = [j for j in snapshot_at(self.db, self.v_revoke)['journey'] if j['person_id'] == 'A']
        self.assertTrue(all(j['dest_shelter'] != 'S2' for j in journeys))
        self.assertTrue(any(j['state'] == 'in_transit' for j in journeys))

    def test_bed_repair_reopens_exact_bed_number(self):
        cap5 = [c for c in snapshot_at(self.db, self.v_plan)['capacity'] if c['shelter_id'] == 'S1'][-1]
        cap9 = [c for c in snapshot_at(self.db, self.v_reopen)['capacity'] if c['shelter_id'] == 'S1'][-1]
        self.assertEqual(cap5['beds'], 2)   # b1 故障，安全床只有 b2、b3
        self.assertEqual(cap9['beds'], 3)   # 晚到修复重开 b1

    def test_fault_targets_actual_bed_not_aggregate_ordering(self):
        snap = snapshot_at(self.db, self.v_counter)
        states = {i['person_id']: i['state'] for i in snap['intake'] if i['incident'] == 'storm-3'}
        self.assertEqual(states, {'A4': 'needs_transfer', 'B4': 'admitted', 'C4': 'admitted'})
        self.assertTrue(any(t['person_id'] == 'A4' and t['basis'] == 'bed_fault_evidence'
                            for t in snap['transfer']))
        # 旧按 intake.at,id 排序会误标最晚入场的 C4；新裁定须留撤回记录
        self.assertTrue(any(r['object_id'] == 'I4C' and r['old_state'] == 'needs_transfer'
                            and r['new_state'] == 'admitted' for r in snap['reversal']))

    def test_supply_allowance_persists_across_shelters_and_journeys(self):
        supplies = [q for q in snapshot_at(self.db, self.v_plan)['supply'] if q['person_id'] == 'A']
        self.assertEqual([q['state'] for q in supplies], ['issued', 'issued', 'duplicate'])
        self.assertEqual({q['shelter_id'] for q in supplies if q['state'] == 'issued'}, {'S1', 'S2'})

    def test_snapshot_history_remains_queryable(self):
        # 旧报表版本可查：v6 的 partial 裁定在后续 rebuild 后仍能从快照读出
        self.assertEqual(historical_verdict(self.db, self.v_req, 'REQ-A-1015', 'b3')['verdict'], 'partial')
        self.assertEqual(historical_verdict(self.db, self.v_video, 'REQ-A-1015', 'b3')['verdict'], 'free')

    def test_replay_audit_matches(self):
        result = audit(self.db, HANDLERS)
        self.assertTrue(result['replay_ok'], result['mismatches'])


class ConflictTest(unittest.TestCase):
    def test_infeasible_journey_reports_minimal_conflict(self):
        db = connect(':memory:')
        import_file(db, 'shelter-import', ROOT / '14a-conflict-beds.json')
        import_file(db, 'intake-import', ROOT / '14b-conflict-intake.json')
        v = db.execute('SELECT MAX(version) AS v FROM adjudication_version').fetchone()['v']
        legs = [j for j in snapshot_at(db, v)['journey'] if j['person_id'] == 'X']
        self.assertEqual(len(legs), 1)
        self.assertEqual(legs[0]['feasible'], 0)
        import json
        conflict = json.loads(legs[0]['conflict'])
        self.assertEqual(conflict['type'], 'exit_after_arrival')
        self.assertEqual(conflict['evidence_ids'], ['ARR-X', 'GATE-X'])  # 最小冲突证据
        db.close()


if __name__ == '__main__':
    unittest.main()
