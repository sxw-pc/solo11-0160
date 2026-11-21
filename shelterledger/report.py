import hashlib
import json
from .store import connect,many
TABLES=['shelter','fault','capacity','capacity_usage','activation','destination','person','intake','exit_record',
        'supply','transfer','bed_card','bed_fault','bed_assignment','gate_record','arrival_record','transfer_plan',
        'evidence_revocation','admission_request','journey','bed_interval','bed_verdict','admission_decision',
        'reversal','adjudication_version','decision_log']
# 版本管理表与报表快照是重裁过程自身的产物（快照自指、reason 记录导入上下文），
# 不参与逐表重放比对；业务裁定表全部参与。
AUDIT_SKIP={'adjudication_version','report_snapshot'}
REBUILD_REASON='evidence batch'
def report(db):
    r={t:[dict(x) for x in many(db,f'SELECT * FROM {t} ORDER BY rowid')] for t in TABLES}
    r['event']=[dict(x) for x in many(db,'SELECT id,kind,sha FROM event ORDER BY rowid')]
    r['source_file']=[dict(x) for x in many(db,'SELECT id,command,sha FROM source_file ORDER BY id')]
    r['report_snapshot']=[dict(x) for x in many(db,'SELECT id,version,payload FROM report_snapshot ORDER BY id')]
    return r
def audit(db,handlers):
    replay=connect(':memory:')
    for src in many(db,'SELECT command,sha,raw FROM source_file ORDER BY id'):
        if hashlib.sha256(src['raw']).hexdigest()!=src['sha']:return {'replay_ok':False,'mismatches':['source_file_sha']}
        with replay:
            for e in json.loads(src['raw'])['events']:
                if e['kind'] not in handlers[src['command']]:raise ValueError('recorded command and kind mismatch')
                if ingest_guard(replay,e):handlers[src['command']][e['kind']](replay,e)
            from . import evidence
            evidence.rebuild(replay,source_command=src['command'],source_sha=src['sha'],reason=REBUILD_REASON)
            replay.execute('INSERT INTO source_file(command,sha,raw) VALUES(?,?,?)',(src['command'],src['sha'],src['raw']))
    old,new=report(db),report(replay)
    for r in (old,new):
        for d in r['decision_log']:d.pop('at',None)
    replay.close()
    mismatches=[k for k in old if k not in AUDIT_SKIP and old[k]!=new[k]]
    return {'replay_ok':not mismatches,'mismatches':mismatches,'event_count':len(old['event'])}
def ingest_guard(db,e):
    from .source import ingest
    return ingest(db,e)
