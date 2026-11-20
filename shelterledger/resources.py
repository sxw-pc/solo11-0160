from .store import one,many,log

def shelter(db,e):
    if int(e['beds'])<0 or not 0<=int(e['accessible'])<=int(e['beds']):raise ValueError('invalid bed inventory')
    old=one(db,'SELECT * FROM shelter WHERE id=?',(e['shelter_id'],))
    if old and int(e['version'])<=old['version']:
        if int(e['version'])==old['version'] and (old['beds'],old['accessible'])!=(int(e['beds']),int(e['accessible'])):
            log(db,'resources',e['shelter_id'],'version_conflict','parallel contradictory resource inventory',e['id'])
            return 'version_conflict'
        return 'stale'
    db.execute('INSERT INTO shelter(id,community,beds,accessible,water,version) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET community=excluded.community,beds=excluded.beds,accessible=excluded.accessible,water=excluded.water,version=excluded.version',
               (e['shelter_id'],e['community'],int(e['beds']),int(e['accessible']),int(bool(e['water'])),int(e['version'])))
    return recompute(db,e['shelter_id'],e['id'])

def fault(db,e):
    if not one(db,'SELECT id FROM shelter WHERE id=?',(e['shelter_id'],)):raise ValueError('unknown shelter')
    db.execute('INSERT INTO fault(id,shelter_id,kind,beds_lost,accessible_lost,at) VALUES(?,?,?,?,?,?)',
               (e['id'],e['shelter_id'],e['fault_kind'],int(e['beds_lost']),int(e['accessible_lost']),e['at']))
    return recompute(db,e['shelter_id'],e['id'])

def repair(db,e):
    f=one(db,'SELECT * FROM fault WHERE id=?',(e['fault_id'],))
    if not f or not e.get('witness') or e['at']<=f['at']:raise ValueError('later witnessed repair required')
    db.execute('UPDATE fault SET closed_at=?,witness=? WHERE id=?',(e['at'],e['witness'],e['fault_id']))
    return recompute(db,f['shelter_id'],e['id'])

def recompute(db,shelter_id,event_id=None):
    s=one(db,'SELECT * FROM shelter WHERE id=?',(shelter_id,))
    faults=many(db,'SELECT * FROM fault WHERE shelter_id=? AND closed_at IS NULL',(shelter_id,))
    water=bool(s['water']) and not any(f['kind']=='water' for f in faults)
    beds=max(0,s['beds']-sum(f['beds_lost'] for f in faults)) if water else 0
    accessible=max(0,min(beds,s['accessible']-sum(f['accessible_lost'] for f in faults))) if water else 0
    state='closed' if beds==0 else 'limited' if beds<s['beds'] or accessible<s['accessible'] else 'open'
    reason='water unavailable' if not water else 'capacity constrained' if state!='open' else 'resources verified'
    old=one(db,'SELECT * FROM capacity WHERE shelter_id=? ORDER BY id DESC LIMIT 1',(shelter_id,))
    if not old or (old['beds'],old['accessible'],old['state'])!=(beds,accessible,state):
        db.execute('INSERT INTO capacity(shelter_id,beds,accessible,water,state,reason) VALUES(?,?,?,?,?,?)',
                   (shelter_id,beds,accessible,int(water),state,reason))
        log(db,'resources',shelter_id,state,reason,event_id)
        from .intake import reconcile_occupancy
        reconcile_occupancy(db,shelter_id)
        from .activation import reconcile_destinations
        reconcile_destinations(db,shelter_id)
    return state
