from .store import one,many,log
from .activation import choose_destination,reconcile_destinations

def person(db,e):
    if not e.get('home_community'):raise ValueError('person home community required')
    db.execute('INSERT INTO person(id,incident,needs_accessible,home_community) VALUES(?,?,?,?)',
               (e['person_id'],e['incident'],int(bool(e.get('needs_accessible'))),e['home_community']))
    return 'registered'

def admit(db,e):
    p=one(db,'SELECT * FROM person WHERE id=? AND incident=?',(e['person_id'],e['incident']))
    if not p:raise ValueError('person must be registered for incident')
    active=one(db,"SELECT * FROM activation WHERE incident=? AND state='active' ORDER BY version DESC LIMIT 1",(e['incident'],))
    existing=one(db,"SELECT * FROM intake WHERE person_id=? AND incident=? AND state IN ('admitted','needs_transfer')",(e['person_id'],e['incident']))
    if existing:state,reason='duplicate','person already occupies a shelter bed'
    elif not active or active['community']!=p['home_community'] or not active['starts']<=e['at']<active['ends']:
        state,reason='blocked','incident order does not cover this person/time'
    else:
        chosen=choose_destination(db,e['incident'],p['needs_accessible'])
        if chosen!=e['shelter_id']:state,reason='diverted',f'priority destination is {chosen or "none"}'
        else:state,reason='admitted','verified capacity and order permit intake'
    db.execute('INSERT INTO intake(id,person_id,incident,shelter_id,at,state,reason) VALUES(?,?,?,?,?,?,?)',
               (e['id'],e['person_id'],e['incident'],e['shelter_id'],e['at'],state,reason))
    log(db,'intake',e['person_id'],state,reason,e['id'])
    if state=='admitted':
        reconcile_destinations(db,e['shelter_id'])
        from .resources import recompute_usage
        recompute_usage(db,e['shelter_id'])
    return state

def leave(db,e):
    i=one(db,'SELECT * FROM intake WHERE id=?',(e['intake_id'],))
    if not i:raise ValueError('unknown intake')
    if i['state'] not in {'admitted','needs_transfer'} or e['at']<=i['at'] or not e.get('witness'):
        state='invalid'
    else:
        state='left'
        db.execute("UPDATE intake SET state='left',reason='witnessed exit' WHERE id=?",(i['id'],))
        reconcile_destinations(db,i['shelter_id'])
        from .resources import recompute_usage
        recompute_usage(db,i['shelter_id'])
    db.execute('INSERT INTO exit_record(id,intake_id,at,witness,state) VALUES(?,?,?,?,?)',(e['id'],i['id'],e['at'],e.get('witness',''),state))
    log(db,'intake',i['person_id'],state,'exit preserves past supply obligation',e['id'])
    return state

def supply(db,e):
    i=one(db,"SELECT * FROM intake WHERE person_id=? AND incident=? AND state='admitted'",(e['person_id'],e['incident']))
    prior=one(db,"SELECT COALESCE(SUM(qty),0) AS q FROM supply WHERE person_id=? AND incident=? AND kind=? AND state='issued'",(e['person_id'],e['incident'],e['supply_kind']))
    if not i or e['at']<i['at']:state,reason='blocked','person not currently admitted'
    elif not e.get('signature'):state,reason='blocked','receipt signature absent'
    elif int(e['qty'])<=0 or prior['q']+int(e['qty'])>2:state,reason='duplicate','per-person incident allowance exceeded'
    else:state,reason='issued','signed supply within incident allowance'
    db.execute('INSERT INTO supply(id,person_id,incident,kind,qty,at,signature,state,reason) VALUES(?,?,?,?,?,?,?,?,?)',
               (e['id'],e['person_id'],e['incident'],e['supply_kind'],int(e['qty']),e['at'],e.get('signature',''),state,reason))
    log(db,'intake',e['person_id'],state,reason,e['id'])
    return state

def reconcile_occupancy(db,shelter_id):
    cap=one(db,'SELECT * FROM capacity WHERE shelter_id=? ORDER BY id DESC LIMIT 1',(shelter_id,))
    active=many(db,"SELECT i.*,p.needs_accessible FROM intake i JOIN person p ON p.id=i.person_id AND p.incident=i.incident WHERE i.shelter_id=? AND i.state IN ('admitted','needs_transfer') ORDER BY i.at,i.id",(shelter_id,))
    accessible_count=0
    for pos,i in enumerate(active):
        if i['needs_accessible']:accessible_count+=1
        if pos>=cap['beds'] or (i['needs_accessible'] and accessible_count>cap['accessible']):
            if i['state']!='needs_transfer':
                db.execute("UPDATE intake SET state='needs_transfer',reason='verified resource capacity shrank' WHERE id=?",(i['id'],))
                target=choose_destination(db,i['incident'],i['needs_accessible'],exclude=shelter_id)
                db.execute('INSERT INTO transfer(person_id,incident,from_shelter,to_shelter,state,reason) VALUES(?,?,?,?,?,?)',
                           (i['person_id'],i['incident'],shelter_id,target,'planned' if target else 'unplaced','resource downgrade requires transfer'))
                log(db,'intake',i['person_id'],'needs_transfer','resource downgrade changed intake decision')
    reconcile_destinations(db,shelter_id)
    from .resources import recompute_usage
    recompute_usage(db,shelter_id)

def reconcile_incident(db,incident):
    active=one(db,"SELECT * FROM activation WHERE incident=? AND state='active' ORDER BY version DESC LIMIT 1",(incident,))
    for i in many(db,"SELECT * FROM intake WHERE incident=? AND state='admitted'",(incident,)):
        covered=active and one(db,"SELECT id FROM destination WHERE activation_id=? AND shelter_id=? AND state IN ('available','full')",(active['id'],i['shelter_id']))
        if not covered:
            db.execute("UPDATE intake SET state='needs_transfer',reason='activation destination no longer authoritative' WHERE id=?",(i['id'],))
            target=choose_destination(db,incident,False,exclude=i['shelter_id']) if active else None
            db.execute('INSERT INTO transfer(person_id,incident,from_shelter,to_shelter,state,reason) VALUES(?,?,?,?,?,?)',
                       (i['person_id'],incident,i['shelter_id'],target,'planned' if target else 'unplaced','activation revision requires destination replan'))
            log(db,'intake',i['person_id'],'needs_transfer','activation revision invalidates old destination')
