"""Explicit, evidence-gated local retention.

No automatic deletion without the protected local policy. A raw partition
is eligible only after both aggregates, backup coverage and incident evidence
are verified. Existing backups are not automatically removed.
"""
import json,sqlite3
from pathlib import Path
from contextlib import contextmanager
from .service_config import safe_path
from .storage import DAY,day_for,day_start

POLICY={"version":1,"enabled":True,"authorized":True,"policy":"balanced-7-30-365-v1"}

def load_retention(cfg):
    p=safe_path(cfg.root,"config/retention.json")
    if not p.exists():return False
    if p.stat().st_size>4096:raise ValueError("retention policy too large")
    d=json.loads(p.read_text(encoding="utf-8"))
    if d=={"version":1,"enabled":False}:return False
    if d!=POLICY:raise ValueError("explicit retention authorization required")
    return True

@contextmanager
def readonly(path):
    p=Path(path).resolve()
    c=sqlite3.connect(p.as_uri()+"?mode=ro&immutable=1",uri=True)
    try:yield c
    finally:c.close()

def _days(store):
    return sorted(m[1] for p in store.root.glob("radio_*.db") if (m:=DAY.fullmatch(p.name)))

def _has_tables(c,names):
    found={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return set(names)<=found

EVIDENCE_TABLES=("rf_incidents","rf_incident_events","rf_incident_reviews","radio_quality_events")

def _incident_evidence(c,backup,start,end):
    if not _has_tables(c,EVIDENCE_TABLES) or not _has_tables(backup,EVIDENCE_TABLES):
        return "incident_schema_missing"
    # Do not age out raw data that intersects the actually observed span
    # of an unresolved incident. A long-stale incident does not hold every
    # subsequent unrelated day indefinitely.
    affected=c.execute("""SELECT incident_id,evidence_rev,ended_at FROM rf_incidents
        WHERE first_at<? AND last_at>=?""",(end,start)).fetchall()
    if any(row[2] is None for row in affected):return "open_incident_evidence"
    for ident,rev,_ in affected:
        local=c.execute("SELECT COUNT(*),MIN(revision),MAX(revision) FROM rf_incident_events WHERE incident_id=?",(ident,)).fetchone()
        if local!=(rev,1,rev):return "incomplete_incident_evidence"
        if c.execute("""SELECT 1 FROM rf_incident_reviews r WHERE r.incident_id=?
           AND NOT EXISTS(SELECT 1 FROM rf_incident_events e WHERE e.incident_id=r.incident_id AND e.revision=r.evidence_rev) LIMIT 1""",(ident,)).fetchone():
            return "incomplete_review_evidence"
        saved=backup.execute("SELECT evidence_rev FROM rf_incidents WHERE incident_id=?",(ident,)).fetchone()
        if not saved or saved[0]<rev:return "incident_backup_behind"
        saved_events=backup.execute("SELECT COUNT(*) FROM rf_incident_events WHERE incident_id=? AND revision<=?",(ident,rev)).fetchone()[0]
        if saved_events!=rev:return "incident_backup_incomplete"
    # Pre-ledger quality records or inconsistent migrations must not be
    # mistaken for evidence preserved elsewhere.
    orphan=c.execute("""SELECT 1 FROM radio_quality_events q WHERE q.observed_at>=? AND q.observed_at<?
       AND q.status IN ('DETECTED','PERSISTENT','RECOVERING','RESOLVED')
       AND NOT EXISTS(SELECT 1 FROM rf_incidents i WHERE i.radio_id=q.radio_id AND i.context=q.context
          AND i.first_at<=q.observed_at AND i.last_at>=q.observed_at) LIMIT 1""",(start,end)).fetchone()
    if orphan:return "legacy_or_unmapped_evidence"
    return None

def _backup_day(backup,day,state,verified_files):
    start=day_start(day);end=start+86400
    with readonly(backup/"core.db") as c:
        saved=c.execute("SELECT revision,completed_revision,raw_count FROM aggregate_manifest WHERE day=?",(day,)).fetchone()
        if saved!=state:return False
    for label in ("5m","1h"):
        name=f"radio_{label}_{day[:6]}.db"
        if name not in verified_files:return False
        p=backup/name
        if not p.is_file():return False
        with readonly(p) as c:
            n=c.execute("SELECT COALESCE(SUM(observed),0) FROM radio_aggregate WHERE bucket>=? AND bucket<?",(start,end)).fetchone()[0]
            if n!=state[2]:return False
    return True

class Retention:
    def __init__(self,store,backup_root,audit_path=None):
        if store.mode!="diagnostic":raise ValueError("diagnostic retention only")
        self.store=store;self.backup_root=Path(backup_root).resolve()
        self.audit_path=Path(audit_path) if audit_path else None

    def run(self,now,aggregate_backup_name=None,max_days=7):
        from .maintenance import verify
        if type(now) is not int or not 1<=max_days<=31:raise ValueError("invalid retention request")
        today=day_start(day_for(now))
        result={"raw_deleted":[],"raw_held":{},"aggregate_rows_deleted":{"5m":0,"1h":0},
                "incidents_deleted":0,"audit_deleted":0,"association_segments_deleted":0,"policy":"balanced-7-30-365-v1"}
        if self.store.pending_count():result["status"]="pending_writes";return result
        raw_days=[d for d in _days(self.store) if day_start(d)+86400<=today-7*86400]
        backup=None;verified_files=set()
        if raw_days and isinstance(aggregate_backup_name,str) and aggregate_backup_name.startswith("aggregate-") and Path(aggregate_backup_name).name==aggregate_backup_name:
            p=self.backup_root/aggregate_backup_name
            if p.is_dir() and not p.is_symlink() and p.resolve().parent==self.backup_root:
                m=verify(p)
                if m.get("mode")=="diagnostic" and m.get("include_aggregates") is True:
                    backup=p;verified_files=set(m['files'])
        with self.store.lock:
            if self.store.pending_count():result["status"]="pending_writes";return result
            ready=[]
            if backup:
                with self.store._db(self.store.root/"core.db") as c,readonly(backup/"core.db") as b:
                    for day in raw_days:
                        if len(ready)>=max_days:break
                        state=c.execute("SELECT revision,completed_revision,raw_count FROM aggregate_manifest WHERE day=?",(day,)).fetchone()
                        if not state or state[0]!=state[1] or state[2] is None:
                            reason="aggregates_incomplete"
                        elif not _backup_day(backup,day,state,verified_files):reason="aggregate_backup_behind"
                        else:reason=_incident_evidence(c,b,day_start(day),day_start(day)+86400)
                        if reason:
                            if len(result["raw_held"])<31:result["raw_held"][day]=reason
                        else:ready.append(day)
                result["raw_deleted"]=self.store.prune_raw(now,ready,days=7)
            else:
                result["raw_held"]={d:"verified_aggregate_backup_required" for d in raw_days[:31]}
            # Never delete aggregate buckets before the corresponding raw day
            # is retired and its file is absent. This also prevents a later
            # aggregate rebuild resurrecting an expired bucket.
            with self.store._db(self.store.root/"core.db") as c:
                retired=[d for d, in c.execute("SELECT day FROM aggregate_manifest WHERE retired=1 ORDER BY day")
                         if not (self.store.root/f"radio_{d}.db").exists()]
            for label,days in (("5m",30),("1h",365)):
                by_month={}
                for day in retired:
                    start=day_start(day)
                    if start+86400<=today-days*86400:by_month.setdefault(day[:6],[]).append((start,start+86400))
                # Each month has at most 31 days; cap deletions per pass.
                remaining=max_days
                for month,ranges in by_month.items():
                    if remaining<=0:break
                    path=self.store.root/f"radio_{label}_{month}.db"
                    if not path.exists():continue
                    with self.store._db(path) as c:
                        c.execute("PRAGMA secure_delete=ON")
                        for start,end in ranges:
                            if remaining<=0:break
                            n=c.execute("DELETE FROM radio_aggregate WHERE bucket>=? AND bucket<?",(start,end)).rowcount
                            if n:
                                remaining-=1;result["aggregate_rows_deleted"][label]+=n
            # Closed incidents are retained >=365 days from their last
            # observation/end or review. Do not remove evidence while raw
            # partitions for the incident are still held.
            cutoff=now-365*86400
            retained_days=[day_start(d) for d in _days(self.store)]
            with self.store._db(self.store.root/"core.db") as c:
                c.execute("PRAGMA secure_delete=ON")
                if _has_tables(c,EVIDENCE_TABLES):
                    candidates=c.execute("""SELECT i.incident_id,i.radio_id,i.context,i.first_at,i.last_at,i.ended_at
                        FROM rf_incidents i WHERE i.ended_at IS NOT NULL AND i.last_at<? AND i.ended_at<?
                        AND NOT EXISTS(SELECT 1 FROM rf_incident_reviews r WHERE r.incident_id=i.incident_id AND r.at>=?)
                        ORDER BY i.ended_at LIMIT 500""",(cutoff,cutoff,cutoff)).fetchall()
                    for ident,rid,ctx,first,last,ended in candidates:
                        if any(d<max(last,ended)+1 and d+86400>first for d in retained_days):continue
                        c.execute("DELETE FROM rf_incident_reviews WHERE incident_id=?",(ident,))
                        c.execute("DELETE FROM rf_incident_events WHERE incident_id=?",(ident,))
                        c.execute("DELETE FROM radio_quality_events WHERE radio_id=? AND context=? AND observed_at BETWEEN ? AND ?",(rid,ctx,first,last))
                        c.execute("DELETE FROM rf_incidents WHERE incident_id=?",(ident,))
                        result["incidents_deleted"]+=1
        history=self.store.root/"association.db"
        if history.is_file():
            with self.store.lock,self.store._db(history) as c:
                c.execute("PRAGMA secure_delete=ON")
                cutoff_history=now-30*86400
                result["association_segments_deleted"]=c.execute("DELETE FROM association_segments WHERE rowid IN (SELECT rowid FROM association_segments WHERE last_at<? ORDER BY last_at LIMIT 50000)",(cutoff_history,)).rowcount
                c.execute("UPDATE association_segments SET first_at=? WHERE first_at<? AND last_at>=?",(cutoff_history,cutoff_history,cutoff_history))
        if self.audit_path and self.audit_path.is_file():
            c=sqlite3.connect(self.audit_path,timeout=2)
            try:
                c.execute("PRAGMA secure_delete=ON")
                with c:
                    result["audit_deleted"]=c.execute("DELETE FROM audit WHERE id IN (SELECT id FROM audit WHERE at<? ORDER BY id LIMIT 10000)",(cutoff,)).rowcount
                c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:c.close()
        result["status"]="ok"
        return result
