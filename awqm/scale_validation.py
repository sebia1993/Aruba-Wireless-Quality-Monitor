"""Isolated, invented CLI fixtures for capacity and elapsed-time validation.

No real target configuration, SSH session, credentials or production storage.
Scale tests measure the real parser/worker/merge/store/read model. They do NOT
establish real WLC latency, Windows SCM lifecycle or browser rendering latency.
"""
import argparse,concurrent.futures,json,math,platform,shutil,sys,tempfile,time
from contextlib import ExitStack
from pathlib import Path
from threading import Event
from .fixture_session import table
from .worker import ControllerWorker
from .engine import Engine
from .storage import Store
from .diagnostic_view import DiagnosticView
from .maintenance import BackgroundMaintenance,Maintenance,write_json

def mac(prefix,n):
    return ":".join([f"{prefix:02x}",*map(lambda x:f"{x:02x}",n.to_bytes(5,"big"))])

def scale_fixtures(index,aps=500,clients_per_ap=10):
    if not 1<=index<=4 or not 1<=aps<=500 or not 2<=clients_per_ap<=10 or clients_per_ap%2:
        raise ValueError("unsupported capacity fixture size")
    inventory=[];active=[];radios=[];association=[];bss=[]
    half=clients_per_ap//2
    for ap in range(aps):
        n=(index-1)*500+ap+1;name=f"DEMO-AP-{n:04d}";ip=f"198.18.{n//256}.{n%256}"
        wired=mac(2,n);wlc=f"192.0.2.{index}"
        inventory.append([name,"DEMO","535",ip,"Up 1d",wlc,"192.0.2.9",wired,"DEMO"])
        active.append([name,ip,"A",f"AP:5GHz-HE:44+/18/24/{half}",f"AP:2.4GHz-HE:6/12/20/{half}"])
        for radio,band,mode,width in ((0,"5","AP:44+","5GHz/HE/40MHz"),(1,"2.4","AP:6","2.4GHz/HE/20MHz")):
            channel="44+" if radio==0 else "6";bs=mac(6,n*2+radio)
            radios.append([name,"DEMO","535",ip,band,mode,"18/24","-94/ 80/ 5" if n%10==0 else "-95/ 25/ 5","1/2/3/4/5/6","0/0/0/0/0/0","0/0/0/0/0/0"])
            bss.append([bs,"DEMO-STAFF",ip,width,channel+"/18/24","ap",half,name,"T","A"])
            for client in range(half):
                association.append([name,bs,mac(10,n*16+radio*8+client),"y","y","DEMO-STAFF","WAB"])
    return {
      "show ap database long":table("AP Database",["Name","Group","AP Type","IP Address","Status","Switch IP","Standby IP","Wired MAC Address","Serial #"],inventory,f"Total APs:{len(inventory)}"),
      "show ap active details":table("Active AP Table",["Name","IP Address","Cluster Role","Radio 0 Band Ch/EIRP/MaxEIRP/Clients","Radio 1 Band Ch/EIRP/MaxEIRP/Clients"],active,f"Num APs:{len(active)}"),
      "show ap radio-summary":table("APs Radios information",["Name","Group","AP Type","IP Address","Band","Mode","EIRP/MaxEIRP","NF/U/I","TD","TM","TC"],radios,f"Total Radios:{len(radios)}"),
      "show ap association":table("Association Table",["Name","bssid","mac","auth","assoc","essid","Flags"],association,f"Num Clients:{len(association)}"),
      "show ap bss-table":table("Aruba AP BSS Table",["bss","ess","ip","band/ht-mode/bandwidth","ch/EIRP/max-eirp","type","cur-cl","ap name","fm","cluster"],bss,f"Num APs:{len(bss)}"),
      "show cpuload":"user 10%, system 10%, idle 80%"
    }

class ScaleSession:
    def __init__(self,responses):self.responses=responses;self.closed=True
    def open(self):self.closed=False
    def run(self,name,target=None,**kw):
        if self.closed or target is not None or name not in self.responses:raise RuntimeError("fixture command unavailable")
        return self.responses[name]
    def close(self):self.closed=True

def workers(clock,wall,aps=500,clients=10,offsets=False):
    out=[]
    for i in range(1,5):
        fixture=scale_fixtures(i,aps,clients)
        out.append(ControllerWorker(f"WLC-{i}",lambda f=fixture:ScaleSession(f),clock,wall,
            mode="standard",offset=(i-1)*15 if offsets else 0,enable_bss=True))
    return out

def p95(values):
    if not values:return None
    values=sorted(values)
    return round(values[max(0,math.ceil(len(values)*.95)-1)],6)

def win11():
    if sys.platform!="win32":return False
    w=sys.getwindowsversion()
    return w.build>=22000 and w.product_type==1 and platform.machine().lower() in ("amd64","x86_64")

def read_benchmark(view,radios):
    # Ten independent concurrent read clients; this excludes HTTP/TLS/DOM.
    def client(i):
        samples=[]
        for _ in range(5):
            start=time.perf_counter();r=view.page("" if i%2==0 else "DEMO-AP-0001",1)
            if not r["available"] or r["total"]!=(radios if i%2==0 else 2):raise RuntimeError("read coverage mismatch")
            samples.append(time.perf_counter()-start)
        return samples
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        times=[x for part in pool.map(client,range(10)) for x in part]
    return {"concurrent_readers":10,"requests":len(times),"p95_seconds":p95(times),"max_seconds":round(max(times),6)}

def scale_test(root,aps=500,clients=10):
    # One complete collection cycle with controlled schedule time. All
    # performance durations are real perf_counter seconds.
    root=Path(root);root.mkdir(parents=True,exist_ok=False)
    fake=[int(time.time())];clock=lambda:fake[0]
    store=Store(root/"data",mode="diagnostic")
    e=Engine(workers(clock,clock,aps,clients),store,site="synthetic-validation",clock=clock,wall=clock)
    expected_radios=aps*8;expected_clients=aps*4*clients
    initial=fake[0];start=time.perf_counter();result={}
    try:
        while time.perf_counter()-start<=60 and fake[0]<initial+30:
            result=e.pulse() or {}
            if e.error:raise RuntimeError("fixture engine storage failed")
            if result.get("mapped_radios")==expected_radios and result.get("active_clients")==expected_clients:break
            time.sleep(.05);fake[0]+=.025
        else:raise RuntimeError("capacity fixture did not reach full coverage")
        elapsed=time.perf_counter()-start
        view=DiagnosticView(store,clock,key=b"synthetic-load-only")
        at=time.perf_counter();view.publish(result,e.published_radios);publish=time.perf_counter()-at
        read=read_benchmark(view,expected_radios)
        snap=store.snapshot()
        if len(snap["radios"])!=expected_radios or snap["pending_writes"]:raise RuntimeError("durable coverage mismatch")
        # No production performance qualification: full-size in-memory
        # read-model checks and one actual ingestion, not 72h or browser tests.
        report={"kind":"synthetic_capacity_cycle","version":__import__("awqm").__version__,"actual_elapsed_seconds":round(elapsed,6),
                "ap_count":aps*4,"radio_count":expected_radios,"client_count":expected_clients,"controllers":4,
                "publish_seconds":round(publish,6),"read_model":read,
                "result":"pass" if read["p95_seconds"]<=2 and elapsed<=60 and publish<=2 else "fail",
                "real_device_validated":False,"scm_validated":False,"soak_validated":False,
                "clock":"controlled_poll_schedule_real_performance_timer"}
        write_json(root/"capacity-result.json",report)
        return report
    finally:e.close()

def soak(root,hours=72,aps=500,clients=10):
    if not win11():raise ValueError("72-hour gate requires an actual Windows 11 x64 client host")
    if type(hours) is not int or hours!=72 or aps!=500 or clients!=10:
        raise ValueError("qualification requires 72 real hours at full fixture capacity")
    root=Path(root);root.mkdir(parents=True,exist_ok=False)
    if shutil.disk_usage(root).free<100*10**9:raise ValueError("at least 100 GB free required for full synthetic soak")
    store=Store(root/"data",mode="diagnostic")
    e=Engine(workers(time.monotonic,time.time,aps,clients,True),store,site="synthetic-soak")
    maintenance=BackgroundMaintenance(Maintenance(store,root/"backups"))
    view=DiagnosticView(store,time.time,key=b"synthetic-soak-only")
    start=time.monotonic();wall_start=time.time();next_check=start+180;previous=start
    checks=0;missed=0;read_max=0;failure=None;last_publish=-1e20
    last_sample=None
    status_path=root/"soak-result.json"
    def record(state):
        report={"kind":"synthetic_full_capacity_real_time_soak","version":__import__("awqm").__version__,
            "state":state,"elapsed_seconds":round(time.monotonic()-start,3),
            "wall_elapsed_seconds":round(time.time()-wall_start,3),"required_seconds":72*3600,
            "checks":checks,"missed_checks":missed,"max_read_model_seconds":round(read_max,6),
            "failure":failure,"last_sample":last_sample,"real_device_validated":False,"scm_validated":False,
            "browser_https_load_validated":False,"windows_11_x64":True,"clock":"real_monotonic_and_wall"}
        write_json(status_path,report);return report
    end=start+72*3600
    def clock_gap(now):
        return now-previous>120 or abs((time.time()-wall_start)-(now-start))>120

    exception_reason="validation_failure"
    try:
        with ExitStack() as resources:
            resources.callback(e.close)
            resources.callback(maintenance.close)
            record("running")
            while True:
                now=time.monotonic()
                if clock_gap(now):
                    failure="clock_or_execution_gap";break
                previous=now
                if now>=end and time.time()-wall_start>=72*3600:
                    if next_check<end:
                        missed+=1
                        failure="missed_validation_interval"
                    break
                result=e.pulse() or e.last_result
                if e.error:failure="engine_storage_failure";break
                m=maintenance.tick()
                if m.get("status") in ("failed","disk_protection","pending_writes"):
                    failure="maintenance_or_disk_failure";break
                if now-last_publish>=5:
                    view.publish(result,e.published_radios);last_publish=now
                if now>=next_check:
                    checks+=1
                    before=time.perf_counter()
                    page=view.page()
                    read_seconds=time.perf_counter()-before
                    read_max=max(read_max,read_seconds)
                    last_sample={k:result.get(k) for k in ("mapped_radios","active_clients","coherent_controllers")}
                    last_sample["published_radios"]=page["total"]
                    if (last_sample["mapped_radios"]!=4000 or last_sample["active_clients"]!=20000
                        or last_sample["coherent_controllers"]!=4 or last_sample["published_radios"]!=4000):
                        missed+=1
                        failure="coverage_mismatch"
                        break
                    if read_seconds>2:
                        failure="read_time_exceeded"
                        break
                    next_check+=60
                    if next_check<=now:failure="missed_validation_interval";break
                    record("running")
                time.sleep(.25)
            if failure is None and maintenance.finish().get("status") in ("failed","disk_protection","pending_writes"):
                failure="maintenance_or_disk_failure"
            if failure is None and (time.monotonic()-start<72*3600 or time.time()-wall_start<72*3600 or checks<4300 or missed or read_max>2):
                failure="duration_or_coverage_gate_not_met"
            exception_reason="cleanup_failure"
        # Waiting for the final maintenance pass and closing workers are part of
        # the trial. The deadline alone must not hide a gap in those operations.
        if failure is None and clock_gap(time.monotonic()):
            failure="clock_or_execution_gap"
    except BaseException as exc:
        if failure is None:
            failure="interrupted" if isinstance(exc,(KeyboardInterrupt,SystemExit)) else exception_reason
        record("incomplete" if failure=="interrupted" else "failed")
        raise
    return record("passed" if failure is None else "failed")

def main():
    p=argparse.ArgumentParser(description="Isolated synthetic validation; never contacts real WLCs")
    p.add_argument("kind",choices=("capacity","https","retained","retained-full","lifecycle","soak"))
    p.add_argument("--output",type=Path,required=True,help="new empty destination path")
    a=p.parse_args()
    if a.kind=="https":
        from .https_validation import https_test
        result=https_test(a.output)
    elif a.kind in ("retained","retained-full"):
        from .retained_validation import retained_test
        result=retained_test(a.output,radios=4000 if a.kind=="retained-full" else 32)
    elif a.kind=="lifecycle":
        from .lifecycle_validation import lifecycle_test
        result=lifecycle_test(a.output)
    else:result=scale_test(a.output) if a.kind=="capacity" else soak(a.output)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if result.get("result",result.get("state")) not in ("pass","passed"):raise SystemExit(1)
