import argparse
import hashlib
import json
from pathlib import Path
from .store import connect
from .source import read_file,ingest
from . import resources,activation,intake,report,crew_source
HANDLERS={
 'crew-import':{'crew_window':crew_source.capture},
 'shelter-import':{'shelter':resources.shelter,'fault':resources.fault,'repair':resources.repair},
 'activation-import':{'order':activation.order,'destination':activation.destination},
 'intake-import':{'person':intake.person,'admit':intake.admit,'leave':intake.leave,'supply':intake.supply},
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
        db.execute('INSERT INTO source_file(command,sha,raw) VALUES(?,?,?)',(command,sha,raw))
    return results
def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('command',choices=[*HANDLERS,'demo','report','audit']);p.add_argument('--db',default='shelter.sqlite');p.add_argument('--file')
    args=p.parse_args(argv);db=connect(args.db)
    if args.command=='demo':
        results=[]
        for cmd,name in [('shelter-import','shelter-ok.json'),('activation-import','activation-ok.json'),('intake-import','intake-ok.json')]:
            results.extend(import_file(db,cmd,Path(__file__).resolve().parents[1]/'fixtures'/name))
        result={'outcomes':results,'report':report.report(db)}
    elif args.command=='report':result=report.report(db)
    elif args.command=='audit':result=report.audit(db,HANDLERS)
    else:
        if not args.file:p.error('--file required')
        result={'outcomes':import_file(db,args.command,args.file),'report':report.report(db)}
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()

