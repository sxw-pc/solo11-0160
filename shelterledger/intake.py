from .store import one,many,log
from .activation import choose_destination,reconcile_destinations

def person(db,e):
    if not e.get('home_community'):raise ValueError('person home community required')
    db.execute('INSERT INTO person(id,incident,needs_accessible,home_community) VALUES(?,?,?,?)',
               (e['person_id'],e['incident'],int(bool(e.get('needs_accessible'))),e['home_community']))
    return 'registered'

def _has_bed_cards(db,shelter_id):
    return bool(one(db,'SELECT 1 FROM bed_card WHERE shelter_id=? AND state=? LIMIT 1',(shelter_id,'active')))

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
    if state=='admitted' and not _has_bed_cards(db,e['shelter_id']):
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
        if not _has_bed_cards(db,i['shelter_id']):
            reconcile_destinations(db,i['shelter_id'])
            from .resources import recompute_usage
            recompute_usage(db,i['shelter_id'])
    db.execute('INSERT INTO exit_record(id,intake_id,at,witness,state) VALUES(?,?,?,?,?)',(e['id'],i['id'],e['at'],e.get('witness',''),state))
    log(db,'intake',i['person_id'],state,'exit preserves past supply obligation',e['id'])
    return state

def supply(db,e):
    # 每人每事件每种物资两份的额度跨点、跨行程累计；转移计划与另点重新入场都不清零
    prior=one(db,"SELECT COALESCE(SUM(qty),0) AS q FROM supply WHERE person_id=? AND incident=? AND kind=? AND state='issued'",(e['person_id'],e['incident'],e['supply_kind']))
    if _has_bed_cards(db,e['shelter_id']) if e.get('shelter_id') else False:
        present_reason=_presence_failure(db,e)
        if present_reason:state,reason='blocked',present_reason
        elif not e.get('signature'):state,reason='blocked','receipt signature absent'
        elif int(e['qty'])<=0 or prior['q']+int(e['qty'])>2:state,reason='duplicate','per-person incident allowance exceeded across all shelters'
        else:state,reason='issued','in-person signed supply within incident allowance'
    else:
        i=one(db,"SELECT * FROM intake WHERE person_id=? AND incident=? AND state='admitted'",(e['person_id'],e['incident']))
        if not i or e['at']<i['at']:state,reason='blocked','person not currently admitted'
        elif not e.get('signature'):state,reason='blocked','receipt signature absent'
        elif int(e['qty'])<=0 or prior['q']+int(e['qty'])>2:state,reason='duplicate','per-person incident allowance exceeded'
        else:state,reason='issued','signed supply within incident allowance'
    db.execute('INSERT INTO supply(id,person_id,incident,kind,qty,at,signature,state,reason,shelter_id) VALUES(?,?,?,?,?,?,?,?,?,?)',
               (e['id'],e['person_id'],e['incident'],e['supply_kind'],int(e['qty']),e['at'],
                e.get('signature',''),state,reason,e.get('shelter_id')))
    log(db,'intake',e['person_id'],state,reason,e['id'])
    return state

def _presence_failure(db,e):
    """本人签领本点物资：床卡/到点接收原件必须证明该时刻本人在该点；
    本人签领同时给实际离点时刻加下界（在 evidence.rebuild 中收紧车闸窗口）。"""
    from .evidence import ts,norm_window
    t=ts(e['at'])
    rows=many(db,"SELECT * FROM bed_assignment WHERE incident=? AND person_id=? AND shelter_id=? AND state IN ('active','moved') ORDER BY at,id",
              (e['incident'],e['person_id'],e['shelter_id']))
    arrival=one(db,"SELECT * FROM arrival_record WHERE incident=? AND person_id=? AND shelter_id=? AND state='open' ORDER BY at_lo DESC,id DESC LIMIT 1",
                (e['incident'],e['person_id'],e['shelter_id']))
    proven_by_assignment=bool(rows) and ts(rows[-1]['at'])<=t
    proven_by_arrival=False
    if arrival:
        A,B=norm_window(arrival['at_lo'],arrival['lo_inclusive'],arrival['at_hi'],arrival['hi_inclusive'])
        proven_by_arrival=A<=t<=B
    if not proven_by_assignment and not proven_by_arrival:
        return 'no bed-card or arrival evidence proves in-person presence at this shelter'
    gate=one(db,"SELECT * FROM gate_record WHERE incident=? AND person_id=? AND shelter_id=? AND direction='out' AND state='open' ORDER BY at_lo DESC,id DESC LIMIT 1",
             (e['incident'],e['person_id'],e['shelter_id']))
    if gate:
        L,H=norm_window(gate['at_lo'],gate['lo_inclusive'],gate['at_hi'],gate['hi_inclusive'])
        if t>=H:return 'gate evidence bounds departure at or before receipt time; presence unproven'
    return None

# ---- 床卡原件：分床、换床 --------------------------------------------------
def bed_assign(db,e):
    if not e.get('witness'):raise ValueError('bed assignment requires witness')
    card=one(db,"SELECT id FROM bed_card WHERE shelter_id=? AND bed_no=? AND state='active'",(e['shelter_id'],str(e['bed_no'])))
    if not card:raise ValueError('target bed is not an active bed card')
    db.execute('INSERT INTO bed_assignment(id,incident,person_id,shelter_id,bed_no,at,witness,state,source) VALUES(?,?,?,?,?,?,?,?,?)',
               (e['id'],e['incident'],e['person_id'],e['shelter_id'],str(e['bed_no']),e['at'],e['witness'],'active',e.get('source','field')))
    return 'assigned'

def bed_move(db,e):
    # 换床：旧床分派以见证时刻闭合（moved），新床开新分派；没有床号就不能换床
    old=one(db,"SELECT * FROM bed_assignment WHERE id=? AND state='active'",(e['from_assignment_id'],))
    if not old:raise ValueError('unknown active source assignment')
    card=one(db,"SELECT id FROM bed_card WHERE shelter_id=? AND bed_no=? AND state='active'",(e['shelter_id'],str(e['bed_no'])))
    if not card:raise ValueError('target bed is not an active bed card')
    if not e.get('witness') or e['at']<=old['at']:raise ValueError('later witnessed move required')
    db.execute("UPDATE bed_assignment SET state='moved' WHERE id=?",(old['id'],))
    db.execute('INSERT INTO bed_assignment(id,incident,person_id,shelter_id,bed_no,at,witness,state,source) VALUES(?,?,?,?,?,?,?,?,?)',
               (e['id'],old['incident'],old['person_id'],e['shelter_id'],str(e['bed_no']),e['at'],e['witness'],'active','bed_move'))
    return 'moved'

# ---- 车闸时窗与到点接收时窗（原件只给时窗，不给单点时刻） ------------------
def gate(db,e):
    if e.get('direction','out')!='out':raise ValueError('only exit gate records supported')
    if not e.get('witness') or e['at_lo']>e['at_hi']:raise ValueError('witnessed exit window required')
    db.execute('INSERT INTO gate_record(id,incident,person_id,shelter_id,direction,at_lo,lo_inclusive,at_hi,hi_inclusive,witness,state) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
               (e['id'],e['incident'],e['person_id'],e['shelter_id'],'out',e['at_lo'],
                int(e.get('lo_inclusive',False)),e['at_hi'],int(e.get('hi_inclusive',True)),e['witness'],'open'))
    return 'gate_recorded'

def arrival(db,e):
    if not e.get('witness') or e['at_lo']>e['at_hi']:raise ValueError('witnessed arrival window required')
    db.execute('INSERT INTO arrival_record(id,incident,person_id,shelter_id,at_lo,lo_inclusive,at_hi,hi_inclusive,witness,state) VALUES(?,?,?,?,?,?,?,?,?,?)',
               (e['id'],e['incident'],e['person_id'],e['shelter_id'],e['at_lo'],
                int(e.get('lo_inclusive',True)),e['at_hi'],int(e.get('hi_inclusive',True)),e['witness'],'open'))
    return 'arrival_recorded'

# ---- 转移计划：计划只是计划，不释放任何床 ----------------------------------
def transfer_plan(db,e):
    if not e.get('witness'):raise ValueError('transfer plan requires witness')
    db.execute('INSERT INTO transfer_plan(id,incident,person_id,from_shelter,to_shelter,bed_no,at,witness,state) VALUES(?,?,?,?,?,?,?,?,?)',
               (e['id'],e['incident'],e['person_id'],e['from_shelter'],e['to_shelter'],
                str(e['bed_no']) if e.get('bed_no') is not None else None,e['at'],e['witness'],'planned'))
    return 'planned'

# ---- 撤证：原件可被后续原件撤销，撤证本身也是原件 --------------------------
_REVOKE_TARGETS={'bed_card':'bed_card','bed_fault':'bed_fault','bed_assignment':'bed_assignment',
                 'gate_record':'gate_record','arrival_record':'arrival_record','transfer_plan':'transfer_plan',
                 'supply':'supply'}
def revoke(db,e):
    table=_REVOKE_TARGETS.get(e['target_kind'])
    if not table:raise ValueError('unsupported revocation target')
    row=one(db,f'SELECT id FROM {table} WHERE id=?',(e['target_id'],))
    if not row:raise ValueError('unknown revocation target')
    if not e.get('witness'):raise ValueError('revocation requires witness')
    db.execute('INSERT INTO evidence_revocation(id,target_kind,target_id,at,witness,reason) VALUES(?,?,?,?,?,?)',
               (e['id'],e['target_kind'],e['target_id'],e['at'],e['witness'],e.get('reason','evidence withdrawn')))
    if table=='bed_card':
        db.execute("UPDATE bed_card SET state='revoked' WHERE id=?",(e['target_id'],))
    elif table=='bed_fault':
        db.execute("UPDATE bed_fault SET state='revoked' WHERE id=?",(e['target_id'],))
    elif table=='bed_assignment':
        db.execute("UPDATE bed_assignment SET state='revoked' WHERE id=?",(e['target_id'],))
    elif table=='gate_record':
        db.execute("UPDATE gate_record SET state='revoked' WHERE id=?",(e['target_id'],))
    elif table=='arrival_record':
        db.execute("UPDATE arrival_record SET state='revoked' WHERE id=?",(e['target_id'],))
    elif table=='transfer_plan':
        db.execute("UPDATE transfer_plan SET state='revoked' WHERE id=?",(e['target_id'],))
    elif table=='supply':
        db.execute("UPDATE supply SET state='revoked' WHERE id=?",(e['target_id'],))
    log(db,'evidence',e['target_id'],'revoked',e.get('reason','evidence withdrawn'),e['id'])
    return 'revoked'

# ---- 现场准入申请：同一份申请同时给甲、乙两份合同分别裁定 ------------------
def admission_request(db,e):
    if e['contract'] not in ('A-realtime','B-contingency'):
        raise ValueError('contract must be A-realtime or B-contingency')
    db.execute('INSERT INTO admission_request(id,incident,person_id,shelter_id,contract,at,needs_accessible) VALUES(?,?,?,?,?,?,?)',
               (e['id'],e['incident'],e['person_id'],e['shelter_id'],e['contract'],e['at'],
                int(bool(e.get('needs_accessible')))))
    return 'request_recorded'

def reconcile_occupancy(db,shelter_id):
    # 旧的“按总量+intake.at 排序标记 needs_transfer”在床卡设施上仍会跑，
    # 但其结论带 capacity_total_ordering 标记，随后由 evidence.rebuild 据床号撤回，
    # 以完整保留“旧结论如何产生、为何撤回”的裁定历史。
    cap=one(db,'SELECT * FROM capacity WHERE shelter_id=? ORDER BY id DESC LIMIT 1',(shelter_id,))
    if not cap:return
    active=many(db,"SELECT i.*,p.needs_accessible FROM intake i JOIN person p ON p.id=i.person_id AND p.incident=i.incident WHERE i.shelter_id=? AND i.state IN ('admitted','needs_transfer') ORDER BY i.at,i.id",(shelter_id,))
    accessible_count=0
    for pos,i in enumerate(active):
        if i['needs_accessible']:accessible_count+=1
        if pos>=cap['beds'] or (i['needs_accessible'] and accessible_count>cap['accessible']):
            if i['state']!='needs_transfer':
                db.execute("UPDATE intake SET state='needs_transfer',reason='verified resource capacity shrank' WHERE id=?",(i['id'],))
                target=choose_destination(db,i['incident'],i['needs_accessible'],exclude=shelter_id)
                db.execute('INSERT INTO transfer(person_id,incident,from_shelter,to_shelter,state,reason,basis) VALUES(?,?,?,?,?,?,?)',
                           (i['person_id'],i['incident'],shelter_id,target,'planned' if target else 'unplaced','resource downgrade requires transfer','capacity_total_ordering'))
                log(db,'intake',i['person_id'],'needs_transfer','resource downgrade changed intake decision')
    if not _has_bed_cards(db,shelter_id):
        reconcile_destinations(db,shelter_id)
        from .resources import recompute_usage
        recompute_usage(db,shelter_id)

def reconcile_incident(db,incident):
    active=one(db,"SELECT * FROM activation WHERE incident=? AND state='active' ORDER BY version DESC LIMIT 1",(incident,))
    for i in many(db,"SELECT * FROM intake WHERE incident=? AND state='admitted'",(incident,)):
        if _has_bed_cards(db,i['shelter_id']):continue
        covered=active and one(db,"SELECT id FROM destination WHERE activation_id=? AND shelter_id=? AND state IN ('available','full')",(active['id'],i['shelter_id']))
        if not covered:
            db.execute("UPDATE intake SET state='needs_transfer',reason='activation destination no longer authoritative' WHERE id=?",(i['id'],))
            target=choose_destination(db,incident,False,exclude=i['shelter_id']) if active else None
            db.execute('INSERT INTO transfer(person_id,incident,from_shelter,to_shelter,state,reason,basis) VALUES(?,?,?,?,?,?,?)',
                       (i['person_id'],incident,i['shelter_id'],target,'planned' if target else 'unplaced','activation revision requires destination replan','activation_revision'))
            log(db,'intake',i['person_id'],'needs_transfer','activation revision invalidates old destination')
