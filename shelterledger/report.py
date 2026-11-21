import hashlib
import json
from .store import connect,many
from .source import ingest
from .carrier_labels import setup
from .carrier_pair_capture import setup as pair_setup
TABLES=['shelter','fault','capacity','capacity_usage','activation','destination','person','intake','exit_record','supply','transfer','decision_log','carrier_sleeve',
        'carrier_pair_round','carrier_image','carrier_code_position','carrier_split_attempt','carrier_split_receipt']
def report(db):
    setup(db)
    pair_setup(db)
    r={t:[dict(x) for x in many(db,f'SELECT * FROM {t} ORDER BY rowid')] for t in TABLES}
    r['event']=[dict(x) for x in many(db,'SELECT id,kind,sha FROM event ORDER BY rowid')]
    r['source_file']=[dict(x) for x in many(db,'SELECT id,command,sha FROM source_file ORDER BY id')]
    return r
def audit(db,handlers):
    replay=connect(':memory:')
    for src in many(db,'SELECT command,sha,raw FROM source_file ORDER BY id'):
        if hashlib.sha256(src['raw']).hexdigest()!=src['sha']:return {'replay_ok':False,'mismatches':['source_file_sha']}
        with replay:
            for e in json.loads(src['raw'])['events']:
                if e['kind'] not in handlers[src['command']]:raise ValueError('recorded command and kind mismatch')
                if ingest(replay,e):handlers[src['command']][e['kind']](replay,e)
            replay.execute('INSERT INTO source_file(command,sha,raw) VALUES(?,?,?)',(src['command'],src['sha'],src['raw']))
    old,new=report(db),report(replay)
    for r in (old,new):
        for d in r['decision_log']:d.pop('at',None)
    replay.close()
    return {'replay_ok':old==new,'mismatches':[k for k in old if old[k]!=new[k]],'event_count':len(old['event'])}

