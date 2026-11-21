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

def _has_bed_cards(db,shelter_id):
    return bool(one(db,'SELECT 1 FROM bed_card WHERE shelter_id=? AND state=? LIMIT 1',(shelter_id,'active')))

def fault(db,e):
    if not one(db,'SELECT id FROM shelter WHERE id=?',(e['shelter_id'],)):raise ValueError('unknown shelter')
    db.execute('INSERT INTO fault(id,shelter_id,kind,beds_lost,accessible_lost,at) VALUES(?,?,?,?,?,?)',
               (e['id'],e['shelter_id'],e['fault_kind'],int(e['beds_lost']),int(e['accessible_lost']),e['at']))
    if _has_bed_cards(db,e['shelter_id']) and e['fault_kind']!='water':
        # 旧总量账照算（beds_lost 直接减总床数），让旧排序结论留下可撤回的痕迹；
        # 但该结论不能决定该转谁——床号裁定由随后的 evidence.rebuild 完成。
        s=one(db,'SELECT * FROM shelter WHERE id=?',(e['shelter_id'],))
        open_faults=many(db,"SELECT * FROM fault WHERE shelter_id=? AND closed_at IS NULL AND kind!='water'",(e['shelter_id'],))
        beds=max(0,s['beds']-sum(f['beds_lost'] for f in open_faults))
        accessible=max(0,min(beds,s['accessible']-sum(f['accessible_lost'] for f in open_faults)))
        state='limited' if beds<s['beds'] else 'open'
        db.execute('INSERT INTO capacity(shelter_id,beds,accessible,water,state,reason) VALUES(?,?,?,?,?,?)',
                   (e['shelter_id'],beds,accessible,1,state,'aggregate beds_lost ledger (not bed-targetable)'))
        log(db,'resources',e['shelter_id'],'aggregate_fault_not_targetable',
            'beds_lost without bed numbers cannot adjudicate bed-card shelter; bed_fault evidence required',e['id'])
        from .intake import reconcile_occupancy
        reconcile_occupancy(db,e['shelter_id'])
        # 不单独产生重裁版本；本批原件落定后由 import_file 统一 rebuild 一版
        return 'aggregate_unresolved'
    return recompute(db,e['shelter_id'],e['id'])

def repair(db,e):
    f=one(db,'SELECT * FROM fault WHERE id=?',(e['fault_id'],))
    if not f or not e.get('witness') or e['at']<=f['at']:raise ValueError('later witnessed repair required')
    db.execute('UPDATE fault SET closed_at=?,witness=? WHERE id=?',(e['at'],e['witness'],e['fault_id']))
    return recompute(db,f['shelter_id'],e['id'])

# ---- 床卡与床级故障原件（独立编号；故障只关闭涉及的床号） ------------------
def bed_card(db,e):
    if one(db,'SELECT id FROM bed_card WHERE shelter_id=? AND bed_no=?',(e['shelter_id'],e['bed_no'])):
        raise ValueError('bed number already registered at shelter')
    if not e.get('witness'):raise ValueError('bed card requires opening witness')
    db.execute('INSERT INTO bed_card(id,shelter_id,bed_no,accessible,opened_at,witness,state) VALUES(?,?,?,?,?,?,?)',
               (e['id'],e['shelter_id'],str(e['bed_no']),int(bool(e.get('accessible'))),e['at'],e['witness'],'active'))
    return 'bed_registered'

def bed_fault(db,e):
    card=one(db,'SELECT id FROM bed_card WHERE shelter_id=? AND bed_no=? AND state=?',
             (e['shelter_id'],str(e['bed_no']),'active'))
    if not card:raise ValueError('bed fault requires an active bed card for the exact bed number')
    if not e.get('witness'):raise ValueError('bed fault requires witness')
    db.execute('INSERT INTO bed_fault(id,shelter_id,bed_no,kind,at,witness,state) VALUES(?,?,?,?,?,?,?)',
               (e['id'],e['shelter_id'],str(e['bed_no']),e.get('fault_kind','structural'),e['at'],e['witness'],'open'))
    return 'bed_fault_recorded'

def bed_repair(db,e):
    f=one(db,"SELECT * FROM bed_fault WHERE id=? AND state='open'",(e['fault_id'],))
    if not f or not e.get('witness') or e['at']<=f['at']:raise ValueError('later witnessed bed repair required')
    db.execute("UPDATE bed_fault SET state='closed',closed_at=?,witness=? WHERE id=?",
               (e['at'],e['witness'],e['fault_id']))
    return 'bed_reopened'

def recompute(db,shelter_id,event_id=None):
    s=one(db,'SELECT * FROM shelter WHERE id=?',(shelter_id,))
    if _has_bed_cards(db,shelter_id):
        # 床卡设施的容量由每批原件落定后的 evidence.rebuild 按安全床号统一重裁，
        # 事件处理中途不单独产生重裁版本，保证一批原件恰好对应一版裁定。
        return 'bed_card_pending_adjudication'
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
        recompute_usage(db,shelter_id)
    return state

def recompute_usage(db,shelter_id):
    if _has_bed_cards(db,shelter_id):
        return  # 床卡设施的占用由 evidence.rebuild 按逐床区间重裁
    cap=one(db,'SELECT * FROM capacity WHERE shelter_id=? ORDER BY id DESC LIMIT 1',(shelter_id,))
    if not cap:return
    admitted=many(db,"SELECT p.needs_accessible FROM intake i JOIN person p ON p.id=i.person_id AND p.incident=i.incident WHERE i.shelter_id=? AND i.state IN ('admitted','needs_transfer')",(shelter_id,))
    remaining=max(0,cap['beds']-len(admitted))
    accessible=max(0,cap['accessible']-sum(x['needs_accessible'] for x in admitted))
    state='full' if remaining==0 else 'limited' if accessible==0 else 'available'
    old=one(db,'SELECT * FROM capacity_usage WHERE shelter_id=? ORDER BY id DESC LIMIT 1',(shelter_id,))
    if not old or (old['remaining_beds'],old['remaining_accessible'],old['state'])!=(remaining,accessible,state):
        db.execute('INSERT INTO capacity_usage(shelter_id,remaining_beds,remaining_accessible,state,reason) VALUES(?,?,?,?,?)',
                   (shelter_id,remaining,accessible,state,'current verified occupancy and resource capacity'))
        log(db,'resources',shelter_id,state,'intake or fault changed remaining resource capacity')
    return state
