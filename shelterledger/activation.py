from .store import one,many,log

def order(db,e):
    if not e.get('commander') or e['starts']>=e['ends'] or int(e['level'])<1:raise ValueError('signed ordered activation required')
    latest=one(db,'SELECT * FROM activation WHERE incident=? ORDER BY version DESC,id DESC LIMIT 1',(e['incident'],))
    if latest and int(e['version'])<latest['version']:state,reason='stale','older activation revision'
    elif latest and int(e['version'])==latest['version']:
        state,reason='conflict','parallel activation at same version'
        db.execute("UPDATE activation SET state='conflict',reason='parallel contradictory activation' WHERE id=?",(latest['id'],))
        for d in many(db,'SELECT * FROM destination WHERE activation_id=?',(latest['id'],)):
            db.execute("UPDATE destination SET state='frozen',reason='activation version conflict' WHERE id=?",(d['id'],))
    else:
        state,reason='active','commander activates incident and community'
        db.execute("UPDATE activation SET state='superseded',reason='newer activation revision' WHERE incident=? AND state='active'",(e['incident'],))
    db.execute('INSERT INTO activation(id,incident,community,level,version,starts,ends,commander,state,reason) VALUES(?,?,?,?,?,?,?,?,?,?)',
               (e['id'],e['incident'],e['community'],int(e['level']),int(e['version']),e['starts'],e['ends'],e['commander'],state,reason))
    log(db,'activation',e['incident'],state,reason,e['id'])
    from .intake import reconcile_incident
    reconcile_incident(db,e['incident'])
    return state

def destination(db,e):
    order=one(db,'SELECT * FROM activation WHERE id=?',(e['activation_id'],))
    shelter=one(db,'SELECT * FROM shelter WHERE id=?',(e['shelter_id'],))
    if not order or not shelter:raise ValueError('activation and shelter required')
    if order['community']!=shelter['community'] and not e.get('cross_area_authority'):
        state,reason='blocked','cross-community destination needs authority'
    elif order['state']!='active':state,reason='frozen','activation order not active'
    else:state,reason='available','destination admitted to evacuation plan'
    db.execute('INSERT INTO destination(id,activation_id,shelter_id,priority,state,reason) VALUES(?,?,?,?,?,?)',
               (e['id'],e['activation_id'],e['shelter_id'],int(e['priority']),state,reason))
    if state=='available':reconcile_destinations(db,e['shelter_id'])
    log(db,'activation',e['id'],state,reason,e['id'])
    return state

def reconcile_destinations(db,shelter_id):
    capacity=one(db,'SELECT * FROM capacity WHERE shelter_id=? ORDER BY id DESC LIMIT 1',(shelter_id,))
    if not capacity:return
    occupied=one(db,"SELECT COUNT(*) AS n FROM intake WHERE shelter_id=? AND state IN ('admitted','needs_transfer')",(shelter_id,))['n']
    for d in many(db,"SELECT * FROM destination WHERE shelter_id=? AND state IN ('available','full','closed')",(shelter_id,)):
        state='closed' if capacity['state']=='closed' else 'full' if occupied>=capacity['beds'] else 'available'
        if state!=d['state']:
            db.execute('UPDATE destination SET state=?,reason=? WHERE id=?',(state,'current verified remaining capacity',d['id']))
            log(db,'activation',d['id'],state,'resource or occupancy revision changed destination')

def choose_destination(db,incident,accessible,exclude=None):
    active=one(db,"SELECT id FROM activation WHERE incident=? AND state='active' ORDER BY version DESC LIMIT 1",(incident,))
    if not active:return None
    for d in many(db,"SELECT * FROM destination WHERE activation_id=? AND state='available' ORDER BY priority,id",(active['id'],)):
        if d['shelter_id']==exclude:continue
        cap=one(db,'SELECT * FROM capacity WHERE shelter_id=? ORDER BY id DESC LIMIT 1',(d['shelter_id'],))
        occupied=one(db,"SELECT COUNT(*) AS n FROM intake WHERE shelter_id=? AND state IN ('admitted','needs_transfer')",(d['shelter_id'],))['n']
        accessible_used=one(db,"SELECT COUNT(*) AS n FROM intake i JOIN person p ON p.id=i.person_id AND p.incident=i.incident WHERE i.shelter_id=? AND i.state IN ('admitted','needs_transfer') AND p.needs_accessible=1",(d['shelter_id'],))['n']
        if occupied<cap['beds'] and (not accessible or accessible_used<cap['accessible']):return d['shelter_id']
    return None
