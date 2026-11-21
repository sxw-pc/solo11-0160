import argparse
import hashlib
import json
from pathlib import Path
from .store import connect
from .source import read_file,ingest
from . import resources,activation,intake,report,evidence
HANDLERS={
 'shelter-import':{'shelter':resources.shelter,'fault':resources.fault,'repair':resources.repair,
                   'bed_card':resources.bed_card,'bed_fault':resources.bed_fault,'bed_repair':resources.bed_repair},
 'activation-import':{'order':activation.order,'destination':activation.destination},
 'intake-import':{'person':intake.person,'admit':intake.admit,'leave':intake.leave,'supply':intake.supply,
                  'bed_assign':intake.bed_assign,'bed_move':intake.bed_move,'gate':intake.gate,
                  'arrival':intake.arrival,'transfer_plan':intake.transfer_plan,'revoke':intake.revoke,
                  'admission_request':intake.admission_request},
}
def import_file(db,command,path):
    raw,events=read_file(path);results=[]
    with db:
        sha=hashlib.sha256(raw).hexdigest()
        if db.execute('SELECT id FROM source_file WHERE command=? AND sha=?',(command,sha)).fetchone():return [{'result':'file_replayed'}]
        for e in events:
            if e['kind'] not in HANDLERS[command]:raise ValueError('wrong subsystem event')
            if ingest(db,e):results.append({'id':e['id'],'result':HANDLERS[command][e['kind']](db,e)})
            else:results.append({'id':e['id'],'result':'event_replayed'})
        # 每批原件落定后按有效原件整体重裁一次（不以导入先后当发生顺序）
        version=evidence.rebuild(db,source_command=command,source_sha=sha,reason=report.REBUILD_REASON)
        db.execute('INSERT INTO source_file(command,sha,raw) VALUES(?,?,?)',(command,sha,raw))
    return [{'rebuild_version':version},*results]

def offline_demo(db):
    """逐秒可核的合成单据验收台：计划未离点、证据收紧、到点撤证、配给跨点保留。"""
    from .evidence import snapshot_at
    root=Path(__file__).resolve().parents[1]/'fixtures'/'evidence'
    sequence=[('shelter-import','01-beds.json'),('activation-import','02-activation.json'),
              ('intake-import','03-intake.json'),('shelter-import','04-fault-and-move.json'),
              ('intake-import','05-plan-gate-supply-arrival.json'),('intake-import','06-contract-requests.json'),
              ('intake-import','07-video-tightening.json'),('intake-import','08-arrival-revocation.json'),
              ('shelter-import','09-bed-reopen.json'),
              ('shelter-import','10-four-bed-beds.json'),('activation-import','11-counterexample-activation.json'),
              ('intake-import','12-counterexample-intake.json'),('shelter-import','13-counterexample-faults.json'),
              ('shelter-import','14a-conflict-beds.json'),('intake-import','14b-conflict-intake.json')]
    out=[]
    sha_to_version={}
    for cmd,name in sequence:
        p=root/name
        if p.exists():
            import_file(db,cmd,p)
            sha=hashlib.sha256(p.read_bytes()).hexdigest()
            sha_to_version[name]=db.execute('SELECT MAX(version) AS v FROM adjudication_version').fetchone()['v']
            out.append({'file':name,'outcomes':'imported'})
    def V(name):return sha_to_version[name]
    def dec(version,request,bed):
        snap=snapshot_at(db,version)
        return [r for r in snap['admission_decision'] if r['request_id']==request and r['bed_no']==bed]
    def verdict(version,request,bed):
        snap=snapshot_at(db,version)
        return next(r for r in snap['bed_verdict'] if r['request_id']==request and r['bed_no']==bed)
    v_plan,v_req,v_video,v_revoke,v_reopen=(V(n) for n in ('05-plan-gate-supply-arrival.json',
        '06-contract-requests.json','07-video-tightening.json','08-arrival-revocation.json','09-bed-reopen.json'))
    scenarios=[
      {'scene':'计划转移不等于实际离点：转移计划不提前释放 b3',
       'version':v_plan,
       'journey_A':[j for j in snapshot_at(db,v_plan)['journey'] if j['person_id']=='A'],
       'note':'A 的 S1 行程腿带车闸时窗与待核区间；b3 在离点窗口内仍可能被占用'},
      {'scene':'10:15 证据收紧前：方案甲待核，方案乙仅列有条件候选（同床同时刻两合同不同决定）',
       'version':v_req,
       'bed_b3_verdict':verdict(v_req,'REQ-A-1015','b3')['verdict'],
       'contract_A_realtime':dec(v_req,'REQ-A-1015','b3'),
       'contract_B_contingency':dec(v_req,'REQ-B-1015','b3')},
      {'scene':'独立视频把离点上界收紧到 10:14 后：10:15 的 b3 所有行程均空，方案甲签实际入场',
       'version':v_video,
       'bed_b3_verdict':verdict(v_video,'REQ-A-1015','b3')['verdict'],
       'contract_A_realtime':dec(v_video,'REQ-A-1015','b3'),
       'contract_B_contingency':dec(v_video,'REQ-B-1015','b3')},
      {'scene':'S2 接收撤证：完整行程退回在途，到达上界消失但已领物资保留',
       'version':v_revoke,
       'journey_A':[j for j in snapshot_at(db,v_revoke)['journey'] if j['person_id']=='A'],
       'supply_A':[q for q in snapshot_at(db,v_revoke)['supply'] if q['person_id']=='A']},
      {'scene':'b1 晚到修复重开：S1 安全床按床号恢复为 3',
       'version':v_reopen,
       'capacity_S1':[c for c in snapshot_at(db,v_reopen)['capacity'] if c['shelter_id']=='S1'][-1]},
      {'scene':'四床反例：beds_lost=2 的旧排序误标 C4，床卡证据改标实际落在故障床 c1 的 A4 并撤回 C4 结论',
       'version':V('13-counterexample-faults.json'),
       'intake_storm3':[i for i in snapshot_at(db,V('13-counterexample-faults.json'))['intake'] if i['incident']=='storm-3'],
       'reversals':snapshot_at(db,V('13-counterexample-faults.json'))['reversal']},
      {'scene':'无法组成完整行程：指出最小冲突证据，不默默选一张单据',
       'version':V('14b-conflict-intake.json'),
       'journey_X':[j for j in snapshot_at(db,V('14b-conflict-intake.json'))['journey'] if j['person_id']=='X']},
    ]
    checkpoints=[{'version':r['version'],'reason':r['reason']}
                 for r in db.execute('SELECT version,reason FROM adjudication_version ORDER BY version')]
    return {'imports':out,'versions':checkpoints,'scenarios':scenarios,
            'snapshots':[dict(r) for r in db.execute('SELECT version FROM report_snapshot ORDER BY version')]}

def main(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument('command',choices=[*HANDLERS,'demo','report','audit','offline-demo','verdicts','journeys','snapshot'])
    p.add_argument('--db',default='shelter.sqlite');p.add_argument('--file');p.add_argument('--version',type=int)
    p.add_argument('--request');p.add_argument('--person')
    args=p.parse_args(argv);db=connect(args.db)
    if args.command=='demo':
        results=[]
        for cmd,name in [('shelter-import','shelter-ok.json'),('activation-import','activation-ok.json'),('intake-import','intake-ok.json')]:
            results.extend(import_file(db,cmd,Path(__file__).resolve().parents[1]/'fixtures'/name))
        result={'outcomes':results,'report':report.report(db)}
    elif args.command=='offline-demo':
        result=offline_demo(db)
    elif args.command=='report':result=report.report(db)
    elif args.command=='audit':result=report.audit(db,HANDLERS)
    elif args.command=='verdicts':
        v=args.version or db.execute('SELECT MAX(version) AS v FROM adjudication_version').fetchone()['v']
        sql='SELECT * FROM bed_verdict WHERE version=?'
        par=[v]
        if args.request:sql+=' AND request_id=?';par.append(args.request)
        result={'version':v,'verdicts':[dict(r) for r in db.execute(sql+' ORDER BY request_id,bed_no',par)]}
    elif args.command=='journeys':
        v=args.version or db.execute('SELECT MAX(version) AS v FROM adjudication_version').fetchone()['v']
        sql='SELECT * FROM journey WHERE version=?';par=[v]
        if args.person:sql+=' AND person_id=?';par.append(args.person)
        result={'version':v,'journeys':[dict(r) for r in db.execute(sql+' ORDER BY person_id,id',par)]}
    elif args.command=='snapshot':
        row=db.execute('SELECT version,payload FROM report_snapshot WHERE version=? ORDER BY version DESC LIMIT 1',
                       (args.version or db.execute('SELECT MAX(version) AS v FROM report_snapshot').fetchone()['v'],)).fetchone()
        result={'version':row['version'],'report':json.loads(row['payload'])} if row else {'error':'no snapshot'}
    else:
        if not args.file:p.error('--file required')
        result={'outcomes':import_file(db,args.command,args.file),'report':report.report(db)}
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
