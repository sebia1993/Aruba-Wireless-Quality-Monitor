"""Bounded maintenance and verified non-destructive restore.

The scope is Radio/core SQLite data only. Job/client history and privacy/evidence
lifecycle are not complete; no automatic deletion or overwrite is permitted.
Run from the single collection coordinator, not from an HTTP request.
"""
import hashlib,json,os,shutil,sqlite3,time
from pathlib import Path
from uuid import uuid4
from .storage import DAY,MONTH,day_for,day_start,disk_level,Store

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

def verified_backup(store,destination,include_aggregates=True,include_raw=False):
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
    if m.get("format")!=1 or m.get("scope")!="radio-core-only" or m.get("mode") not in ("demo","diagnostic"):
        raise ValueError("unsupported backup manifest")
    names=m.get("files")
    hashes=m.get("sha256")
    if not isinstance(names,list) or not isinstance(hashes,dict) or len(names)>10000 or len(set(names))!=len(names) or set(names)!=set(hashes) or "core.db" not in names:
        raise ValueError("invalid manifest file list")
    for name in names:
        if not isinstance(name,str) or not (name=="core.db" or DAY.fullmatch(name) or MONTH.fullmatch(name)):
            raise ValueError("unsafe backup path")
        path=root/name
        if path.is_symlink() or not path.is_file() or digest(path)!=hashes[name]:
            raise ValueError("backup checksum mismatch")
        c=sqlite3.connect(path.as_uri()+"?mode=ro&immutable=1",uri=True)
        try:
            if c.execute("PRAGMA quick_check").fetchall()!=[("ok",)]:
                raise ValueError("backup integrity failure")
            if name=="core.db":
                if c.execute("SELECT value FROM metadata WHERE key='mode'").fetchone()!=(m["mode"],):
                    raise ValueError("backup mode mismatch")
                if c.execute("SELECT COUNT(*) FROM pending").fetchone()[0]:
                    raise ValueError("backup contains incomplete writes")
        finally:c.close()
    return m

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

class Maintenance:
    def __init__(self,store,backup_root,clock=time.time,usage=shutil.disk_usage):
        self.store=store;self.backup_root=Path(backup_root).resolve()
        if self.backup_root==store.root.resolve() or store.root.resolve() in self.backup_root.parents:
            raise ValueError("backup root must be outside data root")
        self.backup_root.mkdir(parents=True,exist_ok=True)
        self.path=store.root/"maintenance.json"
        self.clock=clock;self.usage=usage
        self.state=json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.next_at=0
    def tick(self,force=False):
        """One closed-day rebuild per run; daily core / weekly aggregate backup.

        No automatic pruning: full evidence and privacy lifecycle remain pending.
        No unbounded retry loop. Failures are visible and retried after cooldown.
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
                self.store.aggregate_day(days[0],now)
                self.state["last_aggregated_day"]=days[0]
            weekly=now-self.state.get("last_aggregate_backup",-10**12)>=7*86400
            daily=now-self.state.get("last_core_backup",-10**12)>=86400
            if (weekly or daily) and backup_level not in ("limited","critical"):
                name=("aggregate-" if weekly else "core-")+str(now)+"-"+uuid4().hex[:8]
                dst=verified_backup(self.store,self.backup_root/name,include_aggregates=weekly)
                self.state.update(last_core_backup=now,last_backup=dst.name)
                if weekly:self.state["last_aggregate_backup"]=now
            self.state["status"]="backup_deferred_disk" if (weekly or daily) and backup_level in ("limited","critical") else "ok"
            self.state["retention"]="protected_pending_evidence_lifecycle"
        except (OSError,ValueError,RuntimeError,sqlite3.Error,TypeError) as exc:
            self.state.update(status="failed",error=type(exc).__name__)
        finally:
            write_json(self.path,self.state)
        return dict(self.state)
