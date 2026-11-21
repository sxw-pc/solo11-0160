import sqlite3

SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS source_file(id INTEGER PRIMARY KEY,command TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS event(id TEXT PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS shelter(id TEXT PRIMARY KEY,community TEXT NOT NULL,beds INTEGER NOT NULL,accessible INTEGER NOT NULL,water INTEGER NOT NULL,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS fault(id TEXT PRIMARY KEY,shelter_id TEXT NOT NULL,kind TEXT NOT NULL,beds_lost INTEGER NOT NULL,accessible_lost INTEGER NOT NULL,at TEXT NOT NULL,closed_at TEXT,witness TEXT);
CREATE TABLE IF NOT EXISTS capacity(id INTEGER PRIMARY KEY,shelter_id TEXT NOT NULL,beds INTEGER NOT NULL,accessible INTEGER NOT NULL,water INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS capacity_usage(id INTEGER PRIMARY KEY,shelter_id TEXT NOT NULL,remaining_beds INTEGER NOT NULL,remaining_accessible INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS activation(id TEXT PRIMARY KEY,incident TEXT NOT NULL,community TEXT NOT NULL,level INTEGER NOT NULL,version INTEGER NOT NULL,starts TEXT NOT NULL,ends TEXT NOT NULL,commander TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS destination(id TEXT PRIMARY KEY,activation_id TEXT NOT NULL,shelter_id TEXT NOT NULL,priority INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS person(id TEXT NOT NULL,incident TEXT NOT NULL,needs_accessible INTEGER NOT NULL,home_community TEXT NOT NULL,PRIMARY KEY(id,incident));
CREATE TABLE IF NOT EXISTS intake(id TEXT PRIMARY KEY,person_id TEXT NOT NULL,incident TEXT NOT NULL,shelter_id TEXT NOT NULL,at TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS exit_record(id TEXT PRIMARY KEY,intake_id TEXT NOT NULL,at TEXT NOT NULL,witness TEXT NOT NULL,state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS supply(id TEXT PRIMARY KEY,person_id TEXT NOT NULL,incident TEXT NOT NULL,kind TEXT NOT NULL,qty INTEGER NOT NULL,at TEXT NOT NULL,signature TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,shelter_id TEXT);
CREATE TABLE IF NOT EXISTS transfer(id INTEGER PRIMARY KEY,person_id TEXT NOT NULL,incident TEXT NOT NULL,from_shelter TEXT NOT NULL,to_shelter TEXT,state TEXT NOT NULL,reason TEXT NOT NULL,basis TEXT);
-- 原件层：床卡与床级故障（独立编号，故障只关闭涉及床号）
CREATE TABLE IF NOT EXISTS bed_card(id TEXT PRIMARY KEY,shelter_id TEXT NOT NULL,bed_no TEXT NOT NULL,accessible INTEGER NOT NULL,opened_at TEXT NOT NULL,witness TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'active');
CREATE TABLE IF NOT EXISTS bed_fault(id TEXT PRIMARY KEY,shelter_id TEXT NOT NULL,bed_no TEXT NOT NULL,kind TEXT NOT NULL,at TEXT NOT NULL,closed_at TEXT,witness TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'open');
-- 原件层：分床/换床（换床=旧分派 moved + 新分派）、车闸时窗、到点接收时窗、转移计划
CREATE TABLE IF NOT EXISTS bed_assignment(id TEXT PRIMARY KEY,incident TEXT NOT NULL,person_id TEXT NOT NULL,shelter_id TEXT NOT NULL,bed_no TEXT NOT NULL,at TEXT NOT NULL,witness TEXT NOT NULL,state TEXT NOT NULL,source TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS gate_record(id TEXT PRIMARY KEY,incident TEXT NOT NULL,person_id TEXT NOT NULL,shelter_id TEXT NOT NULL,direction TEXT NOT NULL,at_lo TEXT NOT NULL,lo_inclusive INTEGER NOT NULL,at_hi TEXT NOT NULL,hi_inclusive INTEGER NOT NULL,witness TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'open');
CREATE TABLE IF NOT EXISTS arrival_record(id TEXT PRIMARY KEY,incident TEXT NOT NULL,person_id TEXT NOT NULL,shelter_id TEXT NOT NULL,at_lo TEXT NOT NULL,lo_inclusive INTEGER NOT NULL,at_hi TEXT NOT NULL,hi_inclusive INTEGER NOT NULL,witness TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'open');
CREATE TABLE IF NOT EXISTS transfer_plan(id TEXT PRIMARY KEY,incident TEXT NOT NULL,person_id TEXT NOT NULL,from_shelter TEXT NOT NULL,to_shelter TEXT NOT NULL,bed_no TEXT,at TEXT NOT NULL,witness TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'planned');
CREATE TABLE IF NOT EXISTS evidence_revocation(id TEXT PRIMARY KEY,target_kind TEXT NOT NULL,target_id TEXT NOT NULL,at TEXT NOT NULL,witness TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS admission_request(id TEXT PRIMARY KEY,incident TEXT NOT NULL,person_id TEXT NOT NULL,shelter_id TEXT NOT NULL,contract TEXT NOT NULL,at TEXT NOT NULL,needs_accessible INTEGER NOT NULL DEFAULT 0);
-- 重裁层（每批原件后整体重建；历史只活在版本快照里）
CREATE TABLE IF NOT EXISTS journey(id INTEGER PRIMARY KEY,version INTEGER NOT NULL,incident TEXT NOT NULL,person_id TEXT NOT NULL,origin_shelter TEXT,dest_shelter TEXT,exit_lo INTEGER,exit_hi INTEGER,arrival_lo INTEGER,arrival_hi INTEGER,complete INTEGER NOT NULL,feasible INTEGER NOT NULL,state TEXT NOT NULL,conflict TEXT NOT NULL,evidence_ids TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bed_interval(id INTEGER PRIMARY KEY,version INTEGER NOT NULL,shelter_id TEXT NOT NULL,bed_no TEXT NOT NULL,kind TEXT NOT NULL,lo INTEGER,hi INTEGER,person_id TEXT,incident TEXT,evidence_ids TEXT NOT NULL,journey_key TEXT);
CREATE TABLE IF NOT EXISTS bed_verdict(id INTEGER PRIMARY KEY,version INTEGER NOT NULL,request_id TEXT,shelter_id TEXT NOT NULL,bed_no TEXT NOT NULL,at INTEGER NOT NULL,verdict TEXT NOT NULL,evidence_ids TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS admission_decision(id INTEGER PRIMARY KEY,version INTEGER NOT NULL,request_id TEXT NOT NULL,contract TEXT NOT NULL,shelter_id TEXT NOT NULL,bed_no TEXT,at INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,evidence_ids TEXT NOT NULL,conditions TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reversal(id INTEGER PRIMARY KEY,version INTEGER NOT NULL,object_type TEXT NOT NULL,object_id TEXT NOT NULL,old_state TEXT NOT NULL,new_state TEXT NOT NULL,reason TEXT NOT NULL,evidence_ids TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS adjudication_version(id INTEGER PRIMARY KEY,version INTEGER NOT NULL,source_command TEXT,source_sha TEXT,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS report_snapshot(id INTEGER PRIMARY KEY,version INTEGER NOT NULL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decision_log(id INTEGER PRIMARY KEY,system TEXT NOT NULL,object_id TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,event_id TEXT,at TEXT DEFAULT CURRENT_TIMESTAMP);
"""
DERIVED_TABLES=['journey','bed_interval','bed_verdict','admission_decision','reversal']
def connect(path):
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA)
    # 旧库轻量迁移：配给带上签领点，转移行带上裁定依据
    for table,column,ddl in [('supply','shelter_id','ALTER TABLE supply ADD COLUMN shelter_id TEXT'),
                             ('transfer','basis','ALTER TABLE transfer ADD COLUMN basis TEXT')]:
        cols={r['name'] for r in db.execute(f'PRAGMA table_info({table})')}
        if column not in cols:
            try:db.execute(ddl)
            except sqlite3.OperationalError:pass
    return db
def one(db,sql,args=()):return db.execute(sql,args).fetchone()
def many(db,sql,args=()):return db.execute(sql,args).fetchall()
def log(db,system,key,state,reason,event_id=None):
    db.execute('INSERT INTO decision_log(system,object_id,state,reason,event_id) VALUES(?,?,?,?,?)',(system,key,state,reason,event_id))
