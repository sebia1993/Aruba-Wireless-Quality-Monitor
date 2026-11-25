"""Isolated retained-history read benchmark; no SSH, production DB, or real PII.

Populate exact storage schemas with invented SQL fixtures. This deliberately
bypasses the ingestion spool and aggregation calculation to measure indexed
queries against pre-existing retained data. This is not a write/retention/
backup/SCM/soak qualification.
"""
import json,shutil,time
from pathlib import Path
from .storage import Store,RAW_SCHEMA,AGG_SCHEMA,AGG_COLUMNS,day_for,day_start,disk_level
from .model import Radio
from .diagnostic_view import DiagnosticView
from .maintenance import write_json
from .scale_validation import p95
from . import __version__

FULL=4000
SOURCE="aos8-cli-unvalidated"
# Exactly the approved row-count sizing model: 7 raw days, 30 5m days,
# 365 hourly days (overlapping retention, one epoch per radio).
def expected(radios):
    return {"raw":radios*7*1440,"5m":radios*30*288,"1h":radios*365*24}

def radio(n,at):
    # A unique invented, locally administered wired MAC per physical radio.
    rid=f"retained-fixture:02:00:{n>>16:02x}:{(n>>8)&255:02x}:{n&255:02x}:00:0"
    return Radio(rid,rid.rsplit(":",1)[0],f"DEMO-RETAINED-{n:04d}",f"WLC-{(n-1)%4+1}","5GHz",44,at,40,5,-94,5,
        source=SOURCE,epoch="fixture-epoch-1")

def seed_catalog(c,radios,at):
    c.execute("CREATE TEMP TABLE fixture_radios(n INTEGER PRIMARY KEY, rid TEXT, payload TEXT)")
    # JSON payload matches Radio and is reused via json_set(observed_at).
    c.executemany("INSERT INTO fixture_radios VALUES(?,?,?)",[
        (n,(r:=radio(n,at)).radio_id,json.dumps(vars(r),sort_keys=True,separators=(",",":"))) for n in range(1,radios+1)])

def guard_space(path,full):
    u=shutil.disk_usage(path)
    # Full generation is expensive. Do not silently write through the
    # approved critical/limited disk reserve, even on a dedicated host.
    if full and (u.free<20*10**9 or disk_level(u.free,u.total)!="normal"):
        raise RuntimeError("full retained fixture disk reserve reached")

def seed_raw(store,radios,start,end,full):
    rows=0
    for at in range(start,end,86400):
        guard_space(store.root,full)
        with store._db(store.root/f"radio_{day_for(at)}.db") as c:
            c.execute(RAW_SCHEMA);seed_catalog(c,radios,at)
            # Bounded one-day transaction, physically materialized rows.
            c.execute("""WITH RECURSIVE ticks(i) AS (SELECT 0 UNION ALL SELECT i+1 FROM ticks WHERE i<1439)
              INSERT INTO radio_raw
              SELECT rid,?+i*60,json_set(payload,'$.observed_at',?+i*60)
              FROM fixture_radios CROSS JOIN ticks ORDER BY rid,i""",(at,at))
            n=c.execute("SELECT COUNT(*) FROM radio_raw").fetchone()[0]
            if n!=radios*1440:raise RuntimeError("raw fixture cardinality mismatch")
            rows+=n
        guard_space(store.root,full)
    return rows

def seed_agg(store,radios,start,end,step,label,full):
    # Index and manifest are the production schemas. Populate each month
    # once, with rows ordered by the composite primary key.
    groups={}
    for at in range(start,end,86400):
        groups.setdefault(day_for(at)[:6],[]).append(at)
    rows=0
    for month,days in groups.items():
        guard_space(store.root,full)
        lo=days[0];ticks=len(days)*86400//step;observed=step//60
        with store._db(store.root/f"radio_{label}_{month}.db") as c:
            c.execute(AGG_SCHEMA);seed_catalog(c,radios,lo)
            q="""WITH RECURSIVE ticks(i) AS (SELECT 0 UNION ALL SELECT i+1 FROM ticks WHERE i+1<?)
                INSERT INTO radio_aggregate("""+",".join(AGG_COLUMNS)+""")
                SELECT ?+i*step,rid,'fixture-epoch-1','5GHz',44,?,nobs,nobs,0,
                       40*nobs,40,40,5*nobs,5,5,-94*nobs,-94,-94,5
                FROM (SELECT i,? AS step,? AS nobs FROM ticks) CROSS JOIN fixture_radios
                ORDER BY i,rid"""
            c.execute(q,(ticks,lo,SOURCE,step,observed))
            c.execute("CREATE INDEX IF NOT EXISTS idx_aggregate_radio_time ON radio_aggregate(radio_id,bucket)")
            n=c.execute("SELECT COUNT(*) FROM radio_aggregate").fetchone()[0]
            if n!=radios*ticks:raise RuntimeError("aggregate fixture cardinality mismatch")
            rows+=n
        guard_space(store.root,full)
    return rows

def retained_test(root,radios=32):
    if type(radios) is not int or not 1<=radios<=FULL:raise ValueError("invalid fixture radio count")
    root=Path(root).absolute()
    root.mkdir(parents=True,exist_ok=False)
    full=radios==FULL
    # The full fixture requires dedicated local storage and substantial free
    # space. It may occupy many tens of GB; no automatic cleanup.
    if full and shutil.disk_usage(root).free<150*10**9:
        raise ValueError("full retained fixture requires at least 150 GB free")
    store=Store(root/"data",mode="diagnostic")
    end=day_start(day_for(int(time.time())))
    started=time.perf_counter()
    plan=expected(radios)
    result={"kind":"synthetic_retained_history_read_benchmark","version":__version__,
        "radio_count":radios,"planned_rows":plan,"fixture_rows":{},"result":"incomplete",
        "full_cardinality":full,"full_scale_read_benchmark_passed":False,
        "real_device_validated":False,"ingest_aggregation_validated":False,
        "backup_retention_validated":False,"concurrent_writer_validated":False,
        "browser_rendering_validated":False,"scm_validated":False,"soak_validated":False}
    path=root/"retained-result.json"
    write_json(path,result)
    try:
        # Seed current latest rows via the real writer at today's boundary,
        # outside historical fixture ranges. This adds one raw row per Radio
        # NOT included in the reported historical cardinality.
        for offset in range(1,radios+1,1000):
            store.save_batch([radio(n,end) for n in range(offset,min(offset+1000,radios+1))])
        result["fixture_rows"]["raw"]=seed_raw(store,radios,end-7*86400,end,full)
        result["fixture_rows"]["5m"]=seed_agg(store,radios,end-30*86400,end,300,"5m",full)
        result["fixture_rows"]["1h"]=seed_agg(store,radios,end-365*86400,end,3600,"1h",full)
        if result["fixture_rows"]!=plan:raise RuntimeError("retained cardinality mismatch")
        with store._db(store.root/"core.db") as c:
            c.executemany("INSERT INTO aggregate_manifest(day,revision,completed_revision,raw_count,retired) VALUES(?,1,1,?,?)",
                [(day_for(t),radios*1440,int(t<end-7*86400)) for t in range(end-365*86400,end,86400)])
        result["generation_seconds"]=round(time.perf_counter()-started,6)
        result["data_bytes"]=sum(p.stat().st_size for p in store.root.iterdir() if p.is_file())
        # Read the exact production API read model (without HTTP/DOM).
        # First read per Radio and repeats included; not a cold-cache claim.
        view=DiagnosticView(store,lambda:end,key=b"retained-validation-only")
        view.publish({},[])
        metrics={}
        queries=(("raw",86400,1440,3),("5m",30*86400,8640,5),("1h",365*86400,8760,5))
        for resolution,window,count,limit in queries:
            times=[]
            for n in (1,max(1,radios//4),max(1,radios//2),max(1,radios*3//4),radios):
                token=view.token(radio(n,end).radio_id)
                t=time.perf_counter()
                report=view.history(token,end-window,end,resolution)
                # Include real JSON serialization in measured duration.
                wire=json.dumps(report,ensure_ascii=False,allow_nan=False)
                times.append(time.perf_counter()-t)
                time_field="observed_at" if resolution=="raw" else "bucket"
                step={"raw":60,"5m":300,"1h":3600}[resolution]
                if report["count"]!=count or report["truncated"] or len(report["rows"])!=count:
                    raise RuntimeError("history coverage mismatch")
                if any(r[time_field]!=end-window+i*step for i,r in enumerate(report["rows"])):
                    raise RuntimeError("history gaps or duplicate buckets")
                if radio(n,end).radio_id in wire or not all(r["source"]==SOURCE for r in report["rows"]):
                    raise RuntimeError("history masking/provenance mismatch")
                metric="utilization" if resolution=="raw" else "utilization_avg"
                if any(r[metric]!=40 for r in report["rows"]):raise RuntimeError("history value mismatch")
                if resolution!="raw" and report["coverage"]["valid_samples"]!=window//60:
                    raise RuntimeError("aggregate valid sample count mismatch")
            metrics[resolution]={"requests":len(times),"rows_per_request":count,
                "p95_seconds":p95(times),"max_seconds":round(max(times),6),
                "threshold_seconds":limit,"pass":p95(times)<=limit}
        result["queries"]=metrics
        result["result"]="pass" if all(m["pass"] for m in metrics.values()) else "fail"
        result["full_scale_read_benchmark_passed"]=full and result["result"]=="pass"
        write_json(path,result)
        return result
    except BaseException:
        result["result"]="incomplete";write_json(path,result);raise
