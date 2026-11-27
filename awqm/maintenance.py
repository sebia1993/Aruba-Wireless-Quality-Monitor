"""Bounded maintenance and verified non-destructive restore.

The scope is Radio/core SQLite data. Data and archive expiration each require
their own protected local policy.
Run from the single collection coordinator, not from an HTTP request.
"""
import hashlib,json,os,shutil,sqlite3,time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import uuid4
from .archive_retention import ArchiveRetention
from .storage import DAY,MONTH,day_for,day_start,disk_level,Store,AggregateCancelled,AggregateRevisionChanged

def digest(path):
    h=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""):h.update(chunk)
    return h.hexdigest()

def write_json(path,value):
    tmp=path.with_name(path.name+".tmp-"+uuid4().hex)
    try:
        with tmp.open("w",encoding="utf-8") as f:
            json.dump(value,f,ensure_ascii=False,sort_keys=True,allow_nan=False)
            f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if tmp.exists():tmp.unlink()

def verified_backup(store,destination,include_aggregates=True,include_raw=False,audit_path=None):
    dst=Path(destination).resolve()
    if dst.exists() or dst==store.root.resolve() or store.root.resolve() in dst.parents:
        raise ValueError("new backup directory outside data root required")
    dst.parent.mkdir(parents=True,exist_ok=True)
    stage=dst.parent/(".awqm-stage-"+uuid4().hex)
    stage.mkdir()
    try:
        snap=store.backup(stage/"snapshot",include_raw=include_raw,include_aggregates=include_aggregates)
        manifest=json.loads((snap/"manifest.json").read_text(encoding="utf-8"))
        manifest["format"]=1
        manifest["scope"]="radio-core-only"
        if audit_path is not None and Path(audit_path).is_file():
            source=Path(audit_path).resolve()
            a=sqlite3.connect(source.as_uri()+"?mode=ro",uri=True,timeout=2)
            b=sqlite3.connect(snap/"web-audit.db")
            try:
                a.backup(b)
                if b.execute("PRAGMA quick_check").fetchall()!=[("ok",)]:raise ValueError("audit backup integrity failure")
                b.execute("SELECT id,at,event FROM audit LIMIT 0")
            finally:a.close();b.close()
            manifest["files"].append("web-audit.db")
            manifest["scope"]="radio-core-audit"
        manifest["sha256"]={name:digest(snap/name) for name in manifest["files"]}
        write_json(snap/"manifest.json",manifest)
        if dst.exists():raise FileExistsError(dst)
        snap.rename(dst)
    finally:
        shutil.rmtree(stage,ignore_errors=True)
    return dst

def verify(source):
    root=Path(source).resolve()
    m=json.loads((root/"manifest.json").read_text(encoding="utf-8"))
    if m.get("format")!=1 or m.get("scope") not in ("radio-core-only","radio-core-audit") or m.get("mode") not in ("demo","diagnostic"):
        raise ValueError("unsupported backup manifest")
    names=m.get("files")
    hashes=m.get("sha256")
    if not isinstance(names,list) or not isinstance(hashes,dict) or len(names)>10000 or len(set(names))!=len(names) or set(names)!=set(hashes) or "core.db" not in names:
        raise ValueError("invalid manifest file list")
    if ("web-audit.db" in names)!=(m["scope"]=="radio-core-audit"):raise ValueError("backup scope mismatch")
    for name in names:
        if not isinstance(name,str) or not (name in ("core.db","web-audit.db") or DAY.fullmatch(name) or MONTH.fullmatch(name)):
            raise ValueError("unsafe backup path")
        path=root/name
        if path.is_symlink() or not path.is_file() or digest(path)!=hashes[name]:
            raise ValueError("backup checksum mismatch")
        c=sqlite3.connect(path.as_uri()+"?mode=ro&immutable=1",uri=True)
        try:
            if c.execute("PRAGMA quick_check").fetchall()!=[("ok",)]:
                raise ValueError("backup integrity failure")
            if name=="web-audit.db":
                c.execute("SELECT id,at,event FROM audit LIMIT 0")
            if name=="core.db":
                if c.execute("SELECT value FROM metadata WHERE key='mode'").fetchone()!=(m["mode"],):
                    raise ValueError("backup mode mismatch")
                if c.execute("SELECT COUNT(*) FROM pending").fetchone()[0]:
                    raise ValueError("backup contains incomplete writes")
        finally:c.close()
    return m

def _validate_restored_incident_links(path):
    """Check stored relationship targets before publishing a restored directory.

    Hash/SQLite integrity does not establish logical relationship integrity.
    Older stores without an incident ledger remain supported. Read only the
    copied core, never modify the backup or repair historical evidence.
    """
    from .incidents import IDENT
    db = sqlite3.connect(path.resolve().as_uri()+"?mode=ro&immutable=1", uri=True)
    try:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"rf_incidents", "rf_incident_events"}
        if not tables & required:
            return
        if not required <= tables:
            raise ValueError("restore incident relationship schema incomplete")
        for source, payload in db.execute("""SELECT incident_id,evidence FROM rf_incidents
                UNION ALL SELECT incident_id,evidence FROM rf_incident_events"""):
            try:
                evidence = json.loads(payload)
            except (ValueError, TypeError, RecursionError) as exc:
                raise ValueError("restore incident relationship evidence invalid") from exc
            if not isinstance(evidence, dict):
                raise ValueError("restore incident relationship evidence invalid")
            repeat = evidence.get("repeat_observation")
            if repeat is not None:
                if not isinstance(repeat, dict):
                    raise ValueError("restore repeat incident reference invalid")
                target = repeat.get("prior_incident_id")
                ended = repeat.get("prior_ended_at")
                if (not isinstance(target, str) or not IDENT.fullmatch(target)
                        or target == source or type(ended) is not int):
                    raise ValueError("restore repeat incident reference invalid")
                if db.execute("SELECT ended_at FROM rf_incidents WHERE incident_id=?", (target,)).fetchone() != (ended,):
                    raise ValueError("restore repeat incident target unavailable")
            neighbors = evidence.get("neighbor_evidence", [])
            if not isinstance(neighbors, list):
                raise ValueError("restore peer incident evidence invalid")
            for item in neighbors:
                if not isinstance(item, dict):
                    raise ValueError("restore peer incident evidence invalid")
                if "peer_incident_id" not in item and "peer_incident_revision" not in item:
                    continue
                target = item.get("peer_incident_id")
                revision = item.get("peer_incident_revision")
                if (not isinstance(target, str) or not IDENT.fullmatch(target)
                        or target == source or type(revision) is not int or revision < 1
                        or revision > 9223372036854775807):
                    raise ValueError("restore peer incident reference invalid")
                if not db.execute("""SELECT 1 FROM rf_incidents i
                    JOIN rf_incident_events e ON e.incident_id=i.incident_id
                    WHERE i.incident_id=? AND i.evidence_rev>=? AND e.revision=?""",
                    (target, revision, revision)).fetchone():
                    raise ValueError("restore peer incident revision unavailable")
    finally:
        db.close()


def restore_new(source,destination):
    """Restore to a NEW local directory; never replace an existing database."""
    src=Path(source).resolve();dst=Path(destination).resolve()
    if dst.exists() or src==dst or src in dst.parents:
        raise ValueError("new restore directory outside backup required")
    m=verify(src)
    dst.parent.mkdir(parents=True,exist_ok=True)
    stage=dst.parent/(".awqm-restore-"+uuid4().hex)
    stage.mkdir()
    try:
        for name in m["files"]:
            shutil.copyfile(src/name,stage/name)
            if digest(stage/name)!=m["sha256"][name]:raise ValueError("backup changed during restore")
        if m["mode"] == "diagnostic":
            _validate_restored_incident_links(stage/"core.db")
        # A backup without raw data cannot recreate historical partitions
        # from isolated late-arriving records. Mark those days retired.
        restored=Store(stage,mode=m["mode"])
        with restored._db(stage/"core.db") as c:
            days=[r[0] for r in c.execute("SELECT day FROM aggregate_manifest")]
            for day in days:
                if not (stage/f"radio_{day}.db").is_file():
                    c.execute("UPDATE aggregate_manifest SET retired=1 WHERE day=?",(day,))
        if dst.exists():raise FileExistsError(dst)
        stage.rename(dst)
    except BaseException:
        shutil.rmtree(stage,ignore_errors=True);raise
    return dst

class BackgroundMaintenance:
    """Run one maintenance pass at a time while collection and heartbeat continue."""
    def __init__(self, maintenance):
        self.maintenance = maintenance
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="awqm-maintenance")
        self.pending = None
        self.last = None

    def tick(self):
        if self.pending is not None:
            if self.pending.done():
                return self.finish()
            return {"status":"running"}
        if self.last is not None and self.maintenance.clock() < self.maintenance.next_at:
            return self.last
        self.pending = self.pool.submit(self.maintenance.tick)
        return {"status":"running"}

    def finish(self):
        if self.pending is not None:
            job = self.pending
            self.pending = None
            self.last = job.result()
        return self.last

    def web_status(self, retention_enabled, archive_days):
        """Read-only, bounded summary of the last completed coordinator pass.

        Never serve filesystem paths, archive identifiers, raw incident data,
        exception text or credentials. No disk I/O on the HTTP thread.
        """
        state = self.last or {}
        def timestamp(key):
            value = state.get(key)
            return value if type(value) is int and 0 <= value <= 253402300799 else None
        def level(key):
            value = state.get(key)
            return value if value in ("normal", "limited", "critical", "warning", "unknown") else "unknown"
        allowed = ("ok", "failed", "pending_writes", "disk_protection", "backup_deferred_disk",
                   "revision_changed", "cancelled")
        current = state.get("status")
        return {
            "available": True,
            "status": "running" if self.pending is not None else current if current in allowed else "unknown",
            "last_attempt": timestamp("last_attempt"),
            "last_core_backup": timestamp("last_core_backup"),
            "last_aggregate_backup": timestamp("last_aggregate_backup"),
            "disk_level": level("disk_level"), "backup_disk_level": level("backup_disk_level"),
            "core_backup_interval_days": 1, "aggregate_backup_interval_days": 7,
            "retention_enabled": bool(retention_enabled),
            "retention_days": {"raw": 7, "five_minute": 30, "hourly": 365,
                               "closed_incident": 365, "audit": 365},
            "archive_expiration_days": archive_days,
            "last_retention_attempt": timestamp("last_retention_attempt"),
            "last_archive_retention_attempt": timestamp("last_archive_retention_attempt"),
            "policy_editable": False,
        }

    def close(self):
        self.maintenance.stop_requested.set()
        self.pool.shutdown(wait=True)
        self.finish()


class Maintenance:
    def __init__(self,store,backup_root,clock=time.time,usage=shutil.disk_usage,retention=None,audit_path=None,
                 archive_days=None):
        self.store=store;self.backup_root=Path(backup_root).resolve()
        if self.backup_root==store.root.resolve() or store.root.resolve() in self.backup_root.parents:
            raise ValueError("backup root must be outside data root")
        self.backup_root.mkdir(parents=True,exist_ok=True)
        self.archive_retention = ArchiveRetention(store, backup_root, archive_days) if archive_days is not None else None
        self.path=store.root/"maintenance.json"
        self.clock=clock;self.usage=usage;self.retention=retention;self.audit_path=audit_path
        self.state=json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.next_at=0
        self.stop_requested = Event()
    def tick(self,force=False):
        """One closed-day rebuild per run; daily core / weekly aggregate backup.

        Authorized data and archive expiration run at most once a day.
        Failures are visible and retried after cooldown.
        """
        now=int(self.clock())
        if not force and now<self.next_at:return dict(self.state)
        self.next_at=now+300
        self.state.update(last_attempt=now,error=None)
        try:
            data_usage=self.usage(self.store.root);backup_usage=self.usage(self.backup_root)
            level=disk_level(data_usage.free,data_usage.total)
            backup_level=disk_level(backup_usage.free,backup_usage.total)
            self.state.update(disk_level=level,backup_disk_level=backup_level)
            if self.store.pending_count():
                self.state["status"]="pending_writes";return dict(self.state)
            if level in ("limited","critical"):
                self.state["status"]="disk_protection";return dict(self.state)
            today=day_for(now)
            with self.store.lock,self.store._db(self.store.root/"core.db") as c:
                done={r[0] for r in c.execute("SELECT day FROM aggregate_manifest WHERE completed_revision=revision AND raw_count IS NOT NULL")}
            days=sorted(m[1] for p in self.store.root.glob("radio_*.db") if (m:=DAY.fullmatch(p.name)) and m[1]<today and m[1] not in done)
            if days:
                self.store.aggregate_day(days[0],now,cancel=self.stop_requested)
                self.state["last_aggregated_day"]=days[0]
            weekly=now-self.state.get("last_aggregate_backup",-10**12)>=7*86400
            daily=now-self.state.get("last_core_backup",-10**12)>=86400
            if (weekly or daily) and backup_level not in ("limited","critical"):
                name=("aggregate-" if weekly else "core-")+str(now)+"-"+uuid4().hex[:8]
                dst=verified_backup(self.store,self.backup_root/name,include_aggregates=weekly,audit_path=self.audit_path)
                self.state.update(last_core_backup=now,last_backup=dst.name)
                if weekly:self.state.update(last_aggregate_backup=now,last_aggregate_backup_name=dst.name)
                if self.archive_retention is not None:
                    self.archive_retention.register(dst.name)
            self.state["status"]="backup_deferred_disk" if (weekly or daily) and backup_level in ("limited","critical") else "ok"
            if self.retention is None:
                self.state["retention"]="disabled_pending_local_authorization"
            elif now-self.state.get("last_retention_attempt",-10**12)>=86400:
                self.state["last_retention_attempt"]=now
                self.state["retention"]=self.retention.run(now,self.state.get("last_aggregate_backup_name"))
            if self.archive_retention is None:
                self.state["archive_retention"] = "disabled"
            elif now - self.state.get("last_archive_retention_attempt", -10**12) >= 86400:
                self.state["last_archive_retention_attempt"] = now
                protected = (self.state.get("last_backup"), self.state.get("last_aggregate_backup_name"))
                self.state["archive_retention"] = self.archive_retention.run(now, protected)
        except AggregateRevisionChanged:
            # New observations invalidated this pass. The next tick rebuilds the day.
            self.state.update(status="revision_changed",error=None)
        except AggregateCancelled:
            self.state.update(status="cancelled",error=None)
        except (OSError,ValueError,RuntimeError,sqlite3.Error,TypeError) as exc:
            self.state.update(status="failed",error=type(exc).__name__)
        finally:
            write_json(self.path,self.state)
        return dict(self.state)
