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
CREATE TABLE IF NOT EXISTS supply(id TEXT PRIMARY KEY,person_id TEXT NOT NULL,incident TEXT NOT NULL,kind TEXT NOT NULL,qty INTEGER NOT NULL,at TEXT NOT NULL,signature TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS transfer(id INTEGER PRIMARY KEY,person_id TEXT NOT NULL,incident TEXT NOT NULL,from_shelter TEXT NOT NULL,to_shelter TEXT,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS crew_window(id TEXT PRIMARY KEY,team TEXT NOT NULL,minutes INTEGER NOT NULL,claim INTEGER NOT NULL,witness TEXT NOT NULL,start INTEGER NOT NULL,stop INTEGER NOT NULL,desk TEXT,local_code TEXT,shift TEXT NOT NULL,round INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS crew_evidence(id TEXT PRIMARY KEY,team TEXT NOT NULL,kind TEXT NOT NULL,shift TEXT NOT NULL,round INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,signed_at INTEGER,covers_start INTEGER,covers_stop INTEGER,payload TEXT NOT NULL,event_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS crew_credit(team TEXT NOT NULL,shift TEXT NOT NULL,claim INTEGER NOT NULL,sheet_minutes INTEGER NOT NULL,certified_minutes INTEGER NOT NULL,state TEXT NOT NULL,route TEXT NOT NULL,basis TEXT NOT NULL,missing TEXT NOT NULL,conflicts TEXT NOT NULL,certificate_version INTEGER NOT NULL,PRIMARY KEY(team,shift));
CREATE TABLE IF NOT EXISTS crew_certificate(id INTEGER PRIMARY KEY,team TEXT NOT NULL,shift TEXT NOT NULL,version INTEGER NOT NULL,claim INTEGER NOT NULL,sheet_minutes INTEGER NOT NULL,certified_minutes INTEGER NOT NULL,state TEXT NOT NULL,route TEXT NOT NULL,basis TEXT NOT NULL,missing TEXT NOT NULL,conflicts TEXT NOT NULL,event_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS crew_late_action(id INTEGER PRIMARY KEY,team TEXT NOT NULL,shift TEXT NOT NULL,action TEXT NOT NULL,evidence_id TEXT NOT NULL,event_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decision_log(id INTEGER PRIMARY KEY,system TEXT NOT NULL,object_id TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,event_id TEXT,at TEXT DEFAULT CURRENT_TIMESTAMP);
"""
def connect(path):
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db
def one(db,sql,args=()):return db.execute(sql,args).fetchone()
def many(db,sql,args=()):return db.execute(sql,args).fetchall()
def log(db,system,key,state,reason,event_id=None):
    db.execute('INSERT INTO decision_log(system,object_id,state,reason,event_id) VALUES(?,?,?,?,?)',(system,key,state,reason,event_id))

