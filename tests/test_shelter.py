import json
import tempfile
import unittest
from pathlib import Path
import shelterledger
from shelterledger.store import connect
from shelterledger.__main__ import import_file,HANDLERS
from shelterledger.report import report,audit

ROOT=Path(__file__).resolve().parents[1]

class ShelterTest(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.db=connect(Path(self.temp.name)/'test.sqlite')
    def tearDown(self):self.db.close();self.temp.cleanup()
    def fixture(self,cmd,name):return import_file(self.db,cmd,ROOT/'fixtures'/name)
    def events(self,cmd,events):
        p=Path(self.temp.name)/'events.json';p.write_text(json.dumps({'events':events}));return import_file(self.db,cmd,p)
    def resources(self):self.fixture('shelter-import','shelter-ok.json')
    def activated(self):self.resources();self.fixture('activation-import','activation-ok.json')
    def full(self):self.activated();self.fixture('intake-import','intake-ok.json')
    def test_module_path(self):
        print('module file:',shelterledger.__file__)
        self.assertEqual(Path(shelterledger.__file__).resolve().parents[1],ROOT)
    def test_normal_three_ledgers(self):
        self.full();r=report(self.db)
        self.assertEqual(r['capacity'][0]['beds'],2)
        self.assertEqual(r['activation'][0]['state'],'active')
        self.assertEqual(r['intake'][0]['state'],'admitted')
        self.assertEqual(r['supply'][0]['state'],'issued')
    def test_water_outage_changes_intake_to_transfer(self):
        self.full();self.fixture('shelter-import','shelter-bad.json')
        r=report(self.db)
        self.assertEqual(r['capacity'][-1]['state'],'closed')
        self.assertEqual(r['intake'][0]['state'],'needs_transfer')
        self.assertEqual(r['transfer'][0]['to_shelter'],'hall-b')
    def test_repair_restores_capacity_but_preserves_transfer_history(self):
        self.full();self.fixture('shelter-import','shelter-bad.json')
        self.events('shelter-import',[{'id':'FR1','kind':'repair','fault_id':'F1','at':'2026-04-01T11:00:00','witness':'site-engineer'}])
        r=report(self.db)
        self.assertEqual(r['capacity'][-1]['state'],'open')
        self.assertEqual(len(r['transfer']),1)
    def test_activation_version_conflict_blocks_new_admission(self):
        self.full();self.fixture('activation-import','activation-bad.json')
        r=report(self.db)
        self.assertEqual(r['activation'][0]['state'],'conflict')
        self.assertEqual(r['intake'][0]['state'],'needs_transfer')
        self.events('intake-import',[{'id':'P2','kind':'person','person_id':'resident-2','incident':'storm-1','needs_accessible':False,'home_community':'ward-a'},
          {'id':'I2','kind':'admit','person_id':'resident-2','incident':'storm-1','shelter_id':'hall-b','at':'2026-04-01T10:00:00'}])
        self.assertEqual(report(self.db)['intake'][-1]['state'],'blocked')
    def test_new_activation_revision_requires_destination_replan(self):
        self.full()
        self.events('activation-import',[{'id':'O3','kind':'order','incident':'storm-1','community':'ward-a','level':3,'version':2,'starts':'2026-04-01T08:00:00','ends':'2026-04-02T08:00:00','commander':'chief-a'}])
        r=report(self.db)
        self.assertEqual(r['activation'][-1]['state'],'active')
        self.assertEqual(r['intake'][0]['state'],'needs_transfer')
        self.assertEqual(r['transfer'][-1]['state'],'unplaced')
    def test_duplicate_cross_point_admission_and_supply(self):
        self.full();self.fixture('intake-import','intake-bad.json')
        r=report(self.db)
        self.assertEqual(r['intake'][-1]['state'],'duplicate')
        self.assertEqual(r['supply'][-1]['state'],'duplicate')
    def test_exit_releases_bed_but_preserves_old_supply(self):
        self.full();self.events('intake-import',[{'id':'E1','kind':'leave','intake_id':'I1','at':'2026-04-01T10:00:00','witness':'gate-warden'},
          {'id':'Q3','kind':'supply','person_id':'resident-1','incident':'storm-1','supply_kind':'water-kit','qty':1,'at':'2026-04-01T10:10:00','signature':'resident-sign'}])
        r=report(self.db)
        self.assertEqual(r['intake'][0]['state'],'left')
        self.assertEqual(r['supply'][0]['state'],'issued')
        self.assertEqual(r['supply'][1]['state'],'blocked')
    def test_accessible_demand_cannot_use_ordinary_bed(self):
        self.activated()
        self.events('intake-import',[{'id':'PA','kind':'person','person_id':'access-1','incident':'storm-1','needs_accessible':True,'home_community':'ward-a'},
          {'id':'IA','kind':'admit','person_id':'access-1','incident':'storm-1','shelter_id':'hall-a','at':'2026-04-01T09:00:00'},
          {'id':'PB','kind':'person','person_id':'access-2','incident':'storm-1','needs_accessible':True,'home_community':'ward-a'},
          {'id':'IB','kind':'admit','person_id':'access-2','incident':'storm-1','shelter_id':'hall-a','at':'2026-04-01T09:10:00'}])
        self.assertEqual(report(self.db)['intake'][-1]['state'],'diverted')
    def test_wrong_community_order_does_not_admit(self):
        self.activated()
        self.events('intake-import',[{'id':'PX','kind':'person','person_id':'other','incident':'storm-1','needs_accessible':False,'home_community':'ward-x'},
          {'id':'IX','kind':'admit','person_id':'other','incident':'storm-1','shelter_id':'hall-a','at':'2026-04-01T09:00:00'}])
        self.assertEqual(report(self.db)['intake'][0]['state'],'blocked')
    def test_cross_area_destination_requires_authority(self):
        self.activated()
        self.events('shelter-import',[{'id':'S3','kind':'shelter','shelter_id':'hall-c','community':'ward-b','beds':3,'accessible':1,'water':True,'version':1}])
        self.events('activation-import',[{'id':'D3','kind':'destination','activation_id':'O1','shelter_id':'hall-c','priority':3}])
        self.assertEqual(report(self.db)['destination'][-1]['state'],'blocked')
    def test_occupied_shelter_becomes_full(self):
        self.activated()
        self.events('intake-import',[{'id':'P3','kind':'person','person_id':'r3','incident':'storm-1','needs_accessible':False,'home_community':'ward-a'},
          {'id':'I3','kind':'admit','person_id':'r3','incident':'storm-1','shelter_id':'hall-a','at':'2026-04-01T09:00:00'},
          {'id':'P4','kind':'person','person_id':'r4','incident':'storm-1','needs_accessible':False,'home_community':'ward-a'},
          {'id':'I4','kind':'admit','person_id':'r4','incident':'storm-1','shelter_id':'hall-a','at':'2026-04-01T09:10:00'}])
        self.assertEqual(report(self.db)['destination'][0]['state'],'full')
        usage=[x for x in report(self.db)['capacity_usage'] if x['shelter_id']=='hall-a']
        self.assertEqual(usage[-1]['remaining_beds'],0)
    def test_replay_audit_detects_forged_transfer(self):
        self.full();self.fixture('shelter-import','shelter-bad.json')
        self.assertTrue(audit(self.db,HANDLERS)['replay_ok'])
        self.db.execute("UPDATE transfer SET to_shelter='fake' WHERE id=1");self.db.commit()
        self.assertEqual(audit(self.db,HANDLERS)['mismatches'],['transfer'])
    def test_file_replay_and_conflict(self):
        self.resources();self.assertEqual(self.fixture('shelter-import','shelter-ok.json')[0]['result'],'file_replayed')
        with self.assertRaises(ValueError):self.events('shelter-import',[{'id':'S1','kind':'shelter','shelter_id':'hall-a','community':'ward-b','beds':9,'accessible':1,'water':True,'version':2}])
        self.assertEqual(len(report(self.db)['source_file']),1)

if __name__=='__main__':unittest.main()
