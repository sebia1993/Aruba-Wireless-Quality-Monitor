"""Durable bounded SQLite store with staged multi-file writes.

One Store/Writer instance per data directory. Aggregate rebuilds use a stable
raw snapshot and publish through the revision manifest.
"""
import json
import sqlite3
import shutil
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import Lock, RLock
from uuid import uuid4
import re
from .model import Radio
from .analysis import State, advance, radio_score

KST=timezone(timedelta(hours=9))
DAY=re.compile(r"radio_(\d{8})\.db")
MONTH=re.compile(r"radio_(5m|1h)_(\d{6})\.db")
RAW_SCHEMA="""CREATE TABLE IF NOT EXISTS radio_raw(
    radio_id TEXT NOT NULL,observed_at INTEGER NOT NULL,payload TEXT NOT NULL,
    PRIMARY KEY(radio_id,observed_at))"""
AGG_SCHEMA="""CREATE TABLE IF NOT EXISTS radio_aggregate(
    bucket INTEGER NOT NULL,radio_id TEXT NOT NULL,epoch TEXT NOT NULL,
    band TEXT NOT NULL,channel INTEGER NOT NULL,source TEXT NOT NULL,
    observed INTEGER NOT NULL,valid INTEGER NOT NULL,risky INTEGER NOT NULL,
    utilization_sum REAL,utilization_min REAL,utilization_max REAL,
    interference_sum REAL,interference_min REAL,interference_max REAL,
    noise_sum REAL,noise_min REAL,noise_max REAL,clients_max INTEGER,
    PRIMARY KEY(bucket,radio_id,epoch,band,channel,source))"""
AGG_COLUMNS=("bucket","radio_id","epoch","band","channel","source","observed","valid","risky",
             "utilization_sum","utilization_min","utilization_max",
             "interference_sum","interference_min","interference_max",
             "noise_sum","noise_min","noise_max","clients_max")

def day_for(timestamp):
    if not isinstance(timestamp,int) or isinstance(timestamp,bool) or timestamp<0:
        raise ValueError("invalid timestamp")
    return datetime.fromtimestamp(timestamp,KST).strftime("%Y%m%d")

def day_start(day):
    return int(datetime.strptime(day,"%Y%m%d").replace(tzinfo=KST).timestamp())

def disk_level(free,total):
    """Both absolute and relative thresholds; no silent retention reduction."""
    if total<=0:return "critical"
    if free<=5*10**9 or free/total<=.05:return "critical"
    if free<=10*10**9 or free/total<=.10:return "limited"
    if free<=20*10**9 or free/total<=.20:return "warning"
    return "normal"

class AggregateRevisionChanged(RuntimeError):
    """A late observation invalidated the snapshot being aggregated."""


class AggregateCancelled(RuntimeError):
    """The caller stopped a rebuild before its result was published."""


def _check_cancelled(cancel):
    if cancel is not None and cancel.is_set():
        raise AggregateCancelled("daily aggregation cancelled")


class Store:
    def __init__(self,root,mode="demo",pending_limit=20000,recover=True):
        if mode not in ("demo","diagnostic"):
            raise ValueError("unsupported storage mode")
        self.root=Path(root)
        self.root.mkdir(parents=True,exist_ok=True)
        self.lock=RLock()
        # A backup and another rebuild cannot straddle changes to monthly files.
        # Acquire this lock before the foreground lock and never in the opposite order.
        self._aggregate_lock = Lock()
        self.mode=mode
        self.pending_limit=pending_limit
        with self._db(self.root/"core.db") as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS latest(radio_id TEXT PRIMARY KEY,observed_at INTEGER NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS transitions(radio_id TEXT,observed_at INTEGER,status TEXT,PRIMARY KEY(radio_id,observed_at,status));
            CREATE TABLE IF NOT EXISTS pending(radio_id TEXT,observed_at INTEGER,payload TEXT NOT NULL,
                day TEXT NOT NULL,PRIMARY KEY(radio_id,observed_at));
            CREATE TABLE IF NOT EXISTS aggregate_manifest(day TEXT PRIMARY KEY,revision INTEGER NOT NULL DEFAULT 0,
                completed_revision INTEGER,raw_count INTEGER,retired INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS transition_evidence(radio_id TEXT,observed_at INTEGER,status TEXT,
                payload TEXT NOT NULL,PRIMARY KEY(radio_id,observed_at,status));
            """)
            existing=c.execute("SELECT value FROM metadata WHERE key='mode'").fetchone()
            if existing and existing[0]!=mode:
                raise ValueError("demo/diagnostic database separation required")
            if not existing and mode!="demo" and c.execute("SELECT 1 FROM latest LIMIT 1").fetchone():
                raise ValueError("unclassified existing database; use a new directory")
            c.execute("INSERT OR IGNORE INTO metadata VALUES('mode',?)",(mode,))
        if recover:
            self.recover()

    @contextmanager
    def _db(self,path):
        c=sqlite3.connect(path,timeout=5)
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=FULL")
            with c:
                yield c
        finally:
            c.close()

    def _encode(self,row):
        if not isinstance(row,Radio) or not row.radio_id or not row.ap_id:
            raise ValueError("invalid observation")
        if self.mode=="demo" and row.source!="synthetic":
            raise ValueError("non-synthetic observation in demo store")
        if self.mode=="diagnostic" and row.source=="synthetic":
            raise ValueError("synthetic observation in diagnostic store")
        day=day_for(row.observed_at)
        return (row.radio_id,row.observed_at,json.dumps(asdict(row),ensure_ascii=False,allow_nan=False,sort_keys=True),day)

    @staticmethod
    def _same(a,b):
        # Compatible with older JSON records, whose key order was different.
        return json.loads(a)==json.loads(b)

    def _raw_path(self,day):
        return self.root/f"radio_{day}.db"

    def _preflight(self,items):
        """Check existing raw payloads; return keys already present on disk."""
        by_day={}
        present=set()
        for rid,t,p,day in items:
            by_day.setdefault(day,[]).append((rid,t,p))
        for day,rows in by_day.items():
            path=self._raw_path(day)
            if not path.exists():continue
            with self._db(path) as c:
                for rid,t,p in rows:
                    old=c.execute("SELECT payload FROM radio_raw WHERE radio_id=? AND observed_at=?",(rid,t)).fetchone()
                    if old is not None:
                        if not self._same(old[0],p):
                            raise ValueError("conflicting sample")
                        present.add((rid,t))
        return present

    def save(self,row):
        return self.save_batch([row])>0

    def save_batch(self,rows):
        """Stage before committing raw partitions. Return latest states advanced."""
        items={}
        for row in rows:
            x=self._encode(row)
            key=x[:2]
            if key in items and not self._same(items[key][2],x[2]):
                raise ValueError("conflicting batch sample")
            items[key]=x
            if len(items)>1000:raise ValueError("maximum batch size 1000")
        if not items:return 0
        with self.lock:
            ordered=sorted(items.values(),key=lambda x:(x[1],x[0]))
            present=self._preflight(ordered)
            with self._db(self.root/"core.db") as c:
                pending=c.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
                staged=[]
                for rid,t,p,day in ordered:
                    if c.execute("SELECT retired FROM aggregate_manifest WHERE day=? AND retired=1",(day,)).fetchone():
                        raise ValueError("retired partition cannot be reopened")
                    old=c.execute("SELECT payload FROM pending WHERE radio_id=? AND observed_at=?",(rid,t)).fetchone()
                    if old is not None:
                        if not self._same(old[0],p):
                            raise ValueError("conflicting pending sample")
                        # The original staging transaction already invalidated
                        # the day. Leave the receipt for recovery, even if raw
                        # exists (a crash may have preceded the latest commit).
                        continue
                    if (rid,t) not in present:
                        staged.append((rid,t,p,day))
                # Identical committed or staged samples consume no new queue
                # capacity and must not invalidate a completed aggregate.
                if staged and pending+len(staged)>self.pending_limit:
                    raise RuntimeError("durable queue limit reached")
                c.executemany("INSERT INTO pending VALUES(?,?,?,?)",staged)
                # Invalidate only newly staged days BEFORE any raw write.
                for day in {x[3] for x in staged}:
                    c.execute("""INSERT INTO aggregate_manifest(day,revision) VALUES(?,1)
                      ON CONFLICT(day) DO UPDATE SET revision=revision+1""",(day,))
            return self.recover()

    def recover(self):
        """Replay uncommitted stages. Raw insertion and latest update are idempotent."""
        advanced=0
        with self.lock:
            while True:
                with self._db(self.root/"core.db") as c:
                    items=c.execute("SELECT radio_id,observed_at,payload,day FROM pending ORDER BY observed_at,radio_id LIMIT 1000").fetchall()
                if not items:return advanced
                by_day={}
                for rid,t,p,day in items:
                    by_day.setdefault(day,[]).append((rid,t,p))
                for day,rows in by_day.items():
                    with self._db(self._raw_path(day)) as c:
                        c.execute(RAW_SCHEMA)
                        c.executemany("INSERT OR IGNORE INTO radio_raw VALUES(?,?,?)",rows)
                        for rid,t,p in rows:
                            actual=c.execute("SELECT payload FROM radio_raw WHERE radio_id=? AND observed_at=?",(rid,t)).fetchone()[0]
                            if not self._same(actual,p):
                                # Fail closed; staged work is retained for explicit recovery.
                                raise ValueError("conflicting staged sample")
                with self._db(self.root/"core.db") as c:
                    for rid,t,p,day in items:
                        row=Radio(**json.loads(p))
                        old=c.execute("SELECT observed_at,state FROM latest WHERE radio_id=?",(rid,)).fetchone()
                        if not old or old[0]<t:
                            previous=State(**json.loads(old[1])) if old else State()
                            # A successful diagnostic parse is not a field-validated
                            # production measurement. Preserve observations but do not
                            # create operational incident conclusions.
                            state=(advance(previous,row) if self.mode=="demo" else
                                   State(status="UNKNOWN",last_at=t,epoch=row.epoch))
                            c.execute("""INSERT INTO latest VALUES(?,?,?,?) ON CONFLICT(radio_id) DO UPDATE SET
                              observed_at=excluded.observed_at,payload=excluded.payload,state=excluded.state""",
                              (rid,t,p,json.dumps(asdict(state))))
                            if self.mode=="demo" and previous.status!=state.status:
                                c.execute("INSERT OR IGNORE INTO transitions VALUES(?,?,?)",(rid,t,state.status))
                                # Minimal observation at state transitions. Full incident
                                # evidence/PII lifecycle is not yet implemented.
                                c.execute("INSERT OR IGNORE INTO transition_evidence VALUES(?,?,?,?)",(rid,t,state.status,p))
                            advanced+=1
                        c.execute("DELETE FROM pending WHERE radio_id=? AND observed_at=?",(rid,t))
        return advanced

    def pending_count(self):
        with self.lock,self._db(self.root/"core.db") as c:
            return c.execute("SELECT COUNT(*) FROM pending").fetchone()[0]

    def snapshot(self):
        with self.lock,self._db(self.root/"core.db") as c:
            data=c.execute("SELECT payload,state FROM latest ORDER BY radio_id").fetchall()
            events=c.execute("SELECT radio_id,observed_at,status FROM transitions ORDER BY observed_at DESC LIMIT 100").fetchall()
            pending=c.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
        rows=[]
        for p,s in data:
            row=Radio(**json.loads(p))
            rows.append({**asdict(row),"score":radio_score(row) if self.mode=="demo" else None,**json.loads(s)})
        return {"mode":self.mode,"radios":rows,"events":[dict(zip(("radio_id","observed_at","status"),v)) for v in events],
                "pending_writes":pending}

    @contextmanager
    def _aggregate_slot(self, cancel):
        if cancel is None:
            self._aggregate_lock.acquire()
        else:
            while not self._aggregate_lock.acquire(timeout=0.25):
                _check_cancelled(cancel)
        try:
            _check_cancelled(cancel)
            yield
        except sqlite3.OperationalError as exc:
            if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT and cancel is not None and cancel.is_set():
                raise AggregateCancelled("daily aggregation cancelled") from exc
            raise
        finally:
            self._aggregate_lock.release()

    def aggregate_day(self, day, now, *, cancel=None):
        """Rebuild a closed KST day from one snapshot while collection continues."""
        start=day_start(day)
        if day_for(start)!=day or start+86400>now:
            raise ValueError("only closed days can be aggregated")
        with self._aggregate_slot(cancel):
            path = self._raw_path(day)
            with self.lock:
                if self.pending_count():raise RuntimeError("pending writes must be recovered first")
                if not path.is_file():raise FileNotFoundError("raw partition absent")
                with self._db(self.root/"core.db") as core:
                    core.execute("INSERT OR IGNORE INTO aggregate_manifest(day,revision) VALUES(?,0)",(day,))
                    rev, retired = core.execute(
                        "SELECT revision,retired FROM aggregate_manifest WHERE day=?", (day,)
                    ).fetchone()
                    if retired:
                        raise ValueError("retired partition cannot be rebuilt")
                    # Readers and pruning must exclude this day until both commits succeed.
                    core.execute("UPDATE aggregate_manifest SET completed_revision=NULL WHERE day=?",(day,))
            counts = {}
            with self._db(path) as raw:
                if cancel is not None:
                    raw.set_progress_handler(cancel.is_set, 10000)
                raw.execute("BEGIN")
                expected, outside_day = raw.execute(
                    """SELECT COUNT(*), COUNT(CASE WHEN observed_at < ? OR observed_at >= ? THEN 1 END)
                       FROM radio_raw""", (start, start + 86400)
                ).fetchone()
                if outside_day:
                    raise ValueError("raw partition contains out-of-day records")
                for step, label in ((300, "5m"), (3600, "1h")):
                    _check_cancelled(cancel)
                    destination = self.root / f"radio_{label}_{day[:6]}.db"
                    counts[label] = self._aggregate_resolution(raw, destination, start, step, expected, cancel)
            if counts["5m"]!=counts["1h"]:raise RuntimeError("aggregate counts differ")
            with self.lock, self._db(self.root/"core.db") as c:
                _check_cancelled(cancel)
                published = c.execute(
                    """UPDATE aggregate_manifest SET completed_revision=revision,raw_count=?
                       WHERE day=? AND revision=? AND retired=0""", (counts["5m"], day, rev)
                )
                if published.rowcount != 1:
                    raise AggregateRevisionChanged("closed day changed during aggregation")
            return counts

    def _aggregate_resolution(self, raw, destination, start, step, expected, cancel=None):
        with self._db(destination) as out:
            if cancel is not None:
                out.set_progress_handler(cancel.is_set, 10000)
            out.execute(AGG_SCHEMA)
            out.execute("CREATE INDEX IF NOT EXISTS idx_aggregate_radio_time ON radio_aggregate(radio_id,bucket)")
            out.execute("DELETE FROM radio_aggregate WHERE bucket>=? AND bucket<?",(start,start+86400))
            # Materialization evaluates the JSON fields and validity once per sample.
            # Raw row order preserves the accumulation order for late observations.
            query = """WITH samples AS MATERIALIZED (
              SELECT (observed_at / ?) * ? AS bucket,radio_id,
                json_extract(payload,'$.epoch') epoch,json_extract(payload,'$.band') band,
                json_extract(payload,'$.channel') channel,json_extract(payload,'$.source') source,
                json_extract(payload,'$.utilization') u,json_extract(payload,'$.interference') i,
                json_extract(payload,'$.noise') nf,json_extract(payload,'$.clients') clients,
                CASE WHEN json_extract(payload,'$.complete')=1
                  AND json_type(payload,'$.utilization') IN ('integer','real')
                  AND json_type(payload,'$.interference') IN ('integer','real')
                  AND json_type(payload,'$.noise') IN ('integer','real')
                  AND json_extract(payload,'$.utilization') BETWEEN 0 AND 100
                  AND json_extract(payload,'$.interference') BETWEEN 0 AND 100
                  AND json_extract(payload,'$.noise') BETWEEN -130 AND 0 THEN 1 ELSE 0 END ok
              FROM radio_raw WHERE radio_id=? ORDER BY rowid)
              SELECT bucket,radio_id,epoch,band,channel,source,COUNT(*),SUM(ok),
                SUM(CASE WHEN ok=1 AND (u>=75 OR i>=30 OR nf>-80) THEN 1 ELSE 0 END),
                SUM(CASE WHEN ok=1 THEN u END),MIN(CASE WHEN ok=1 THEN u END),MAX(CASE WHEN ok=1 THEN u END),
                SUM(CASE WHEN ok=1 THEN i END),MIN(CASE WHEN ok=1 THEN i END),MAX(CASE WHEN ok=1 THEN i END),
                SUM(CASE WHEN ok=1 THEN nf END),MIN(CASE WHEN ok=1 THEN nf END),MAX(CASE WHEN ok=1 THEN nf END),
                MAX(CASE WHEN ok=1 AND clients>=0 THEN clients END)
              FROM samples GROUP BY bucket,radio_id,epoch,band,channel,source"""
            total = 0
            insert = "INSERT INTO radio_aggregate VALUES(" + ",".join("?" for _ in AGG_COLUMNS) + ")"
            # The raw primary key streams distinct radios in index order.
            # Limit SQLite's temporary grouping and JSON extraction to one radio.
            for (radio_id,) in raw.execute("SELECT DISTINCT radio_id FROM radio_raw"):
                _check_cancelled(cancel)
                cursor = raw.execute(query, (step, step, radio_id))
                while chunk := cursor.fetchmany(1000):
                    out.executemany(insert, chunk)
                    total += sum(row[6] for row in chunk)
            if total != expected:
                raise RuntimeError("aggregate coverage mismatch")
            return total

    def history(self,radio_id,start,end,resolution="raw",limit=1000):
        if not 1<=limit<=10000 or end<=start or end-start>366*86400:
            raise ValueError("invalid history bounds")
        rows=[]
        with self.lock:
            if resolution=="raw":
                for path in sorted(self.root.glob("radio_*.db")):
                    m=DAY.fullmatch(path.name)
                    if not m:continue
                    d=day_start(m[1])
                    if d>=end or d+86400<=start:continue
                    with self._db(path) as c:
                        for p, in c.execute("SELECT payload FROM radio_raw WHERE radio_id=? AND observed_at>=? AND observed_at<? ORDER BY observed_at LIMIT ?",
                                              (radio_id,start,end,limit-len(rows))):
                            rows.append(json.loads(p))
                    if len(rows)>=limit:break
            elif resolution in ("5m","1h"):
                # Do not serve half-written or invalidated aggregate days. A late
                # raw arrival requires rebuild before that day is visible again.
                with self._db(self.root/"core.db") as c:
                    days=[r[0] for r in c.execute("SELECT day FROM aggregate_manifest WHERE completed_revision=revision AND raw_count IS NOT NULL ORDER BY day")]
                # Open a monthly partition only once for a contiguous run of
                # completed days. Never bridge invalidated/missing days.
                ranges={}
                for day in days:
                    d=day_start(day)
                    if d>=end or d+86400<=start:continue
                    lo=max(start,d);hi=min(end,d+86400)
                    group=ranges.setdefault(day[:6],[])
                    if group and group[-1][1]==lo:group[-1]=(group[-1][0],hi)
                    else:group.append((lo,hi))
                q="SELECT "+",".join(AGG_COLUMNS)+" FROM radio_aggregate WHERE radio_id=? AND bucket>=? AND bucket<? ORDER BY bucket,epoch,band,channel,source LIMIT ?"
                for month,intervals in ranges.items():
                    path=self.root/f"radio_{resolution}_{month}.db"
                    if not path.is_file():continue
                    with self._db(path) as c:
                        for lo,hi in intervals:
                            for r in c.execute(q,(radio_id,lo,hi,limit-len(rows))):
                                row=dict(zip(AGG_COLUMNS,r))
                                for key in ("utilization","interference","noise"):
                                    row[key+"_avg"]=row[key+"_sum"]/row["valid"] if row["valid"] else None
                                rows.append(row)
                            if len(rows)>=limit:break
                    if len(rows)>=limit:break
            else:raise ValueError("unsupported resolution")
        return rows

    def prune_raw(self,now,evidence_ready_days=(),days=7):
        """Explicit evidence approval required until full incident preservation is implemented.

        Never delete open days, incomplete aggregate partitions, pending work,
        or unapproved evidence. No automatic destructive cleanup in this build.
        """
        if days<7:raise ValueError("approved minimum raw retention is seven days")
        cutoff=day_start(day_for(now))-days*86400
        ready=set(evidence_ready_days)
        deleted=[]
        with self.lock:
            if self.pending_count():raise RuntimeError("pending writes must be recovered")
            for path in sorted(self.root.glob("radio_*.db")):
                m=DAY.fullmatch(path.name)
                if not m:continue
                day=m[1]
                if day not in ready or day_start(day)+86400>cutoff:continue
                with self._db(self.root/"core.db") as c:
                    state=c.execute("SELECT revision,completed_revision,raw_count FROM aggregate_manifest WHERE day=?",(day,)).fetchone()
                if not state or state[0]!=state[1] or state[2] is None:continue
                # Check both actual monthly files and aggregate counts before retiring raw.
                good=True
                for label in ("5m","1h"):
                    agg=self.root/f"radio_{label}_{day[:6]}.db"
                    if not agg.exists():good=False;break
                    with self._db(agg) as c:
                        n=c.execute("SELECT COALESCE(SUM(observed),0) FROM radio_aggregate WHERE bucket>=? AND bucket<?",(day_start(day),day_start(day)+86400)).fetchone()[0]
                    if n!=state[2]:good=False;break
                if not good:continue
                with self._db(path) as c:
                    if c.execute("SELECT COUNT(*) FROM radio_raw").fetchone()[0]!=state[2]:continue
                    busy,_,_=c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if busy or Path(str(path)+"-wal").exists():continue
                # Mark first; interrupted deletion leaves a safe extra file rather
                # than a partition that can be recreated without its old evidence.
                with self._db(self.root/"core.db") as c:
                    c.execute("UPDATE aggregate_manifest SET retired=1 WHERE day=?",(day,))
                path.unlink()
                deleted.append(day)
        return deleted

    def backup(self,destination,include_raw=False,include_aggregates=True):
        """Consistent, checked online SQLite backup to a new directory.

        No copying of a live main DB file. Serialized, explicit maintenance.
        A restored diagnostic DB must remain protected by the operator's ACLs.
        """
        dest=Path(destination).resolve()
        if dest.exists() or dest==self.root.resolve() or self.root.resolve() in dest.parents:
            raise ValueError("choose a new backup directory outside data root")
        dest.parent.mkdir(parents=True,exist_ok=True)
        temp=dest.parent/(dest.name+".partial-"+uuid4().hex)
        with self._aggregate_lock, self.lock:
            if self.pending_count():raise RuntimeError("pending writes must be recovered")
            temp.mkdir()
            try:
                paths=[self.root/"core.db"]+[p for p in self.root.glob("radio_*.db")
                    if (include_aggregates and MONTH.fullmatch(p.name)) or (include_raw and DAY.fullmatch(p.name))]
                for src in paths:
                    with self._db(src) as a:
                        b=sqlite3.connect(temp/src.name)
                        try:
                            a.backup(b)
                            if b.execute("PRAGMA quick_check").fetchone()[0]!="ok":
                                raise RuntimeError("backup integrity failure")
                        finally:b.close()
                (temp/"manifest.json").write_text(json.dumps({"mode":self.mode,"include_raw":include_raw,"include_aggregates":include_aggregates,
                    "files":[p.name for p in paths],"created_at":datetime.now(timezone.utc).isoformat()},indent=2),encoding="utf-8")
                temp.rename(dest)
            except Exception:
                shutil.rmtree(temp,ignore_errors=True)
                raise
        return dest
