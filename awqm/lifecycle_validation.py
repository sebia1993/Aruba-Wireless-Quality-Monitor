"""Representative actual SQLite ingestion/recovery/backup/retirement trial.

Invented observations, isolated fresh directories, no SSH or site data.
Measure real durations without claiming full-cardinality qualification.
"""
import time
from pathlib import Path
from .model import Radio
from .storage import Store,day_for,day_start
from .maintenance import verified_backup,verify,restore_new,write_json
from .scale_validation import p95
from . import __version__


class _Interrupted(RuntimeError):
    pass


class _FixtureStore(Store):
    # Fail after the real save_batch staging transaction, before replay.
    interrupt=False

    def recover(self):
        if self.interrupt:
            self.interrupt=False
            raise _Interrupted("simulated interruption after durable staging")
        return super().recover()


def _radio(n,at):
    ident=f"lifecycle-fixture-{n:04d}"
    return Radio(ident,ident,f"DEMO-LIFE-{n:04d}",f"WLC-{(n-1)%4+1}",
                 "5GHz",44,at,40,5,-94,5,source="aos8-cli-unvalidated",epoch="fixture-1")


def _require(condition,message):
    if not condition:
        raise RuntimeError(message)


def lifecycle_test(root,radios=32,minutes=120):
    if type(radios) is not int or not 1<=radios<=128:
        raise ValueError("representative fixture requires 1..128 radios")
    if type(minutes) is not int or not 5<=minutes<=1440 or minutes%5:
        raise ValueError("minutes must be a multiple of five, 5..1440")
    root=Path(root).absolute()
    root.mkdir(parents=True,exist_ok=False)
    start=day_start(day_for(int(time.time())))-86400
    day=day_for(start)
    end=start+minutes*60
    expected=radios*minutes
    report={"kind":"synthetic_representative_storage_lifecycle","version":__version__,
            "radios":radios,"minutes":minutes,"expected_samples":expected,
            "result":"incomplete","full_cardinality_validated":False,
            "retained_full_validated":False,"real_device_validated":False,
            "scm_validated":False,"soak_validated":False,"concurrent_writer_validated":False}
    result_file=root/"lifecycle-result.json"
    write_json(result_file,report)
    try:
        store=_FixtureStore(root/"data",mode="diagnostic")
        times=[]
        for i in range(minutes):
            batch=[_radio(n,start+i*60) for n in range(1,radios+1)]
            tick=time.perf_counter()
            if i==minutes//2:
                store.interrupt=True
                try:
                    store.save_batch(batch)
                except _Interrupted:
                    pass
                else:
                    raise RuntimeError("fixture interruption not triggered")
                _require(store.pending_count()==radios,"staged receipt count mismatch")
                # New instance performs real durable recovery; no raw insertion
                # or core receipt is synthesized by this harness.
                store=Store(root/"data",mode="diagnostic")
                _require(store.pending_count()==0,"pending receipt recovery failed")
                _require(all(r["observed_at"]==start+i*60 for r in store.snapshot()["radios"]),
                         "recovered latest mismatch")
                _require(store.save_batch(batch)==0,"recovered sample replay advanced latest")
                report["interrupted_stage_recovered"]=True
            else:
                _require(store.save_batch(batch)==radios,"writer coverage mismatch")
            times.append(time.perf_counter()-tick)
        report["write_cycle_p95_seconds"]=p95(times)
        report["write_cycle_max_seconds"]=round(max(times),6)
        _require(store.pending_count()==0,"unrecovered receipts")
        tick=time.perf_counter()
        counts=store.aggregate_day(day,start+86400)
        report["aggregation_seconds"]=round(time.perf_counter()-tick,6)
        _require(counts=={"5m":expected,"1h":expected},"aggregate coverage mismatch")
        report["aggregate_sample_counts"]=counts
        # Verify exact rows and bucket values for every fixture Radio, not
        # merely total row counts. Final hourly bucket may be partial.
        def inspect(s):
            snapshots={}
            for n in range(1,radios+1):
                ident=_radio(n,start).radio_id
                raw=s.history(ident,start,end,"raw",limit=2000)
                _require([r["observed_at"] for r in raw]==list(range(start,end,60)),
                         "raw gaps, duplicates or timestamp mismatch")
                _require(all(r["utilization"]==40 and r["source"]=="aos8-cli-unvalidated" for r in raw),
                         "raw values or provenance mismatch")
                series={}
                for resolution,step in (("5m",300),("1h",3600)):
                    rows=s.history(ident,start,end,resolution,limit=2000)
                    _require([r["bucket"] for r in rows]==list(range(start,end,step)),
                             "aggregate bucket coverage mismatch")
                    for r in rows:
                        count=min(step,end-r["bucket"])//60
                        _require(r["observed"]==r["valid"]==count and r["risky"]==0 and
                                 r["utilization_avg"]==40 and r["source"]=="aos8-cli-unvalidated",
                                 "aggregate value or valid count mismatch")
                    series[resolution]=rows
                snapshots[ident]=series
            return snapshots

        series=inspect(store)
        # Replaying an already aggregated row cannot invalidate this day.
        _require(store.save_batch([_radio(n,start) for n in range(1,radios+1)])==0,
                 "duplicate replay advanced latest")
        _require(inspect(store)==series,"duplicate replay changed history")
        report["duplicate_preserved_aggregate"]=True
        tick=time.perf_counter()
        backup=verified_backup(store,root/"backup",include_raw=True,include_aggregates=True)
        manifest=verify(backup)
        report["backup_seconds"]=round(time.perf_counter()-tick,6)
        _require(manifest["include_raw"] and manifest["include_aggregates"],"backup scope mismatch")
        tick=time.perf_counter()
        restored=Store(restore_new(backup,root/"restored"),mode="diagnostic")
        report["restore_seconds"]=round(time.perf_counter()-tick,6)
        _require(restored.snapshot()==store.snapshot() and inspect(restored)==series,
                 "restored history or latest differs")
        report["backup_restore_verified"]=True
        # Exercise destructive retention only on the separately restored
        # synthetic copy and require explicit evidence-ready day approval.
        now=start+9*86400
        _require(restored.prune_raw(now)==[],"unapproved raw retirement")
        _require(restored.prune_raw(now,[day])==[day],"approved raw retirement failed")
        for ident,expected_series in series.items():
            _require(restored.history(ident,start,end,"raw",limit=2000)==[],
                     "retired raw history still present")
            for resolution,rows in expected_series.items():
                _require(restored.history(ident,start,end,resolution,limit=2000)==rows,
                         "retirement changed aggregate evidence")
        try:
            restored.save(_radio(1,start))
        except ValueError:
            pass
        else:
            raise RuntimeError("retired day was reopened")
        _require(inspect(store)==series,"source data changed during restore or retirement")
        report["raw_retirement_guard_verified"]=True
        report["result"]="pass"
        write_json(result_file,report)
        return report
    except BaseException:
        report["result"]="incomplete"
        write_json(result_file,report)
        raise
