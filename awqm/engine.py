"""Independent worker orchestration and single-writer collection coordinator.

This module is exercised by injected fixture sessions. No production web
listener, credentials, device access, or Windows service is started here.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict,replace
from threading import Lock
import time
import sqlite3
from .aos8 import merge_radios,merge_associations
from .durable_jobs import DurableJobs
from .selection import Selector
from .client_history import ClientHistory,map_samples
import json

class Engine:
    def __init__(self,workers,store,site="local-trial",clock=time.monotonic,wall=time.time,simulation=False,durable_jobs=False,auto=False,quality_ledger=None,live_clients=None,cluster_monitor=None):
        self.workers={w.controller:w for w in workers}
        if not 1<=len(workers)<=4 or len(self.workers)!=len(workers):
            raise ValueError("one to four unique workers required")
        if store.mode!=("demo" if simulation else "diagnostic"):raise ValueError("separate mode storage required")
        if auto and not simulation:raise ValueError("automatic analysis requires validated data; demo only")
        self.simulation=simulation;self.selector=Selector() if auto else None
        if durable_jobs:
            for w in workers:
                w.jobs=DurableJobs(store.root/"jobs.db",w.controller,now=wall())
        self.store=store;self.site=site;self.clock=clock;self.wall=wall
        self.client_history=ClientHistory(store.root/"client_history.db",simulation=True) if simulation else None
        self.detail_pending={}
        self.client_stats_last={}
        self.active_detail_clients=[]
        self.pool=ThreadPoolExecutor(max_workers=len(workers),thread_name_prefix="awqm-wlc")
        self.inflight={};self.snapshots={};self.outcomes={};self.next_tick={}
        self.accepted={};self.last_result={};self.error=None
        self.paused=False;self.closed=False;self.lock=Lock()
        self.published_radios=()
        self.quality_ledger=quality_ledger;self.field_quality={}
        self.live_clients=live_clients
        if simulation and live_clients:raise ValueError("live client path is separate from demo")
        self.live_details={};self.live_client_views=[]
        self.cluster_monitor=cluster_monitor;self.cluster_frames={};self.cluster_view=None
    @staticmethod
    def _step(w,cluster_load=False):
        r=w.tick(cluster_load=cluster_load) if w.enable_cluster else w.tick()
        snapshot=w.snapshot()
        b=w.latest.get("show ap bss-table")
        a=w.latest.get("show ap association")
        # Only a successfully refreshed, same-generation metadata observation.
        if not b or b.generation!=w.generation or w.outcomes[b.command].status!="success" or not 0<=w.wall()-b.observed_at<=330:b=None
        if not a or a.generation!=w.generation or w.outcomes[a.command].status!="success" or not 0<=w.wall()-a.observed_at<=150:a=None
        return r,snapshot,b,a,w.cluster_frame()
    def submit(self,controller,request_id,name,target,ttl=300):
        # Caller must authenticate/authorize before using this internal API.
        if self.closed:raise RuntimeError("engine closed")
        return self.workers[controller].submit_detail(request_id,name,target,ttl)
    def cancel(self,controller,job_id):
        w=self.workers[controller]
        return w.jobs.cancel(job_id,w.wall())
    def pulse(self):
        """Nonblocking relative to SSH workers; SQLite commit is serialized here.

        Failed storage freezes new collection dispatch rather than accumulating
        an unbounded queue. An explicit successful recover resumes scheduling.
        """
        if not self.lock.acquire(blocking=False):return None
        try:
            if self.closed:raise RuntimeError("engine closed")
            now=self.clock()
            for name,f in list(self.inflight.items()):
                if not f.done():continue
                del self.inflight[name]
                try:
                    reading,snapshot,bss,assoc,frame=f.result()
                    if frame is not None:self.cluster_frames[name]=frame
                    self.snapshots[name]=snapshot
                    if self.client_history and reading and reading.status=="success" and reading.target:
                        self.detail_pending[name]=(reading,snapshot,bss,assoc)
                    if self.live_clients and reading and reading.target and (reading.status=="success" or reading.command=="show ap debug client-stats"):
                        self.live_details[name]=(reading,snapshot,bss,assoc)
                    if reading:self.outcomes[name]={"command":reading.command,"status":reading.status,"observed_at":reading.observed_at}
                except Exception as exc:
                    self.snapshots[name]=None
                    self.cluster_frames.pop(name,None)
                    self.outcomes[name]={"status":"worker_error","reason":type(exc).__name__}
                    # Avoid a hot failure loop.
                    self.next_tick[name]=now+15
                    if isinstance(exc, (sqlite3.Error,OSError)):
                        self.error=type(exc).__name__
            active=[]
            for name,s in self.snapshots.items():
                if s is None:continue
                # Never relabel an expired cached snapshot as a current reading.
                w=self.workers[name];wall=int(self.wall())
                max_radio=360 if w.schedule.mode=="safe" else 90
                if not 0<=wall-s.observed_at<=max_radio:continue
                if s.anchor_at is None or not 0<=wall-s.anchor_at<=150:continue
                if s.inventory_at is None or not 0<=wall-s.inventory_at<=660:continue
                active.append(s)
            radios,unresolved=merge_radios(active,self.site)
            clients,conflicts=merge_associations(active)
            if self.simulation:radios=[replace(r,source="synthetic") for r in radios]
            fresh=[r for r in radios if r.observed_at>self.accepted.get(r.radio_id,-1)]
            if not self.error:
                try:
                    for i in range(0,len(fresh),1000):
                        batch=fresh[i:i+1000]
                        self.store.save_batch(batch)
                        self.accepted.update({r.radio_id:r.observed_at for r in batch})
                except Exception as exc:
                    self.error=type(exc).__name__
            if self.selector and not self.error and not self.paused and fresh:
                # Only current merged samples, not old persisted or conflicted APs.
                current={(r.radio_id,r.observed_at) for r in fresh}
                try:
                    self.selector.plan([r for r in self.store.snapshot()["radios"]
                        if (r["radio_id"],r["observed_at"]) in current],self.wall(),self.workers)
                except (sqlite3.Error,OSError) as exc:
                    self.error=type(exc).__name__
            if self.client_history and not self.error:
                try:
                    current_states={r["radio_id"]:r for r in self.store.snapshot()["radios"]}
                    persistent={k for k,r in current_states.items() if r["status"]=="PERSISTENT" and r["open_incident"]}
                    for name,(reading,snap,bss,assoc) in list(self.detail_pending.items()):
                        if not 0<=self.wall()-reading.observed_at<=90:
                            del self.detail_pending[name];continue
                        if not snap or not bss or not assoc:
                            del self.detail_pending[name];continue
                        # Association mapping is checked against all current coherent
                        # controller snapshots; conflicts are not silently resolved.
                        claims={r["client_mac"]:r for r in assoc.data if r["active"] and r["client_mac"] in clients
                            and all(r[k]==clients[r["client_mac"]][k] for k in ("ap_name","bssid","ssid"))}
                        sid=self.workers[name].instance_id+":"+str(reading.generation)
                        if reading.command=="show ap debug client-table":
                            mapped=map_samples(reading.data,name,reading.target,sid,reading.observed_at,claims,bss.data,radios)
                            self.client_history.ingest(mapped,persistent)
                            # At most three active low-quality clients per AP table
                            # round, separate Station Stats jobs; do not infer cause.
                            recent={c["client_mac"]:c for c in self.client_history.latest(self.wall())}
                            eligible=[s for s,r,_ in mapped if s.radio_id in persistent and recent.get(s.client_mac,{}).get("activity")=="active"
                                and recent.get(s.client_mac,{}).get("quality") is not None and recent[s.client_mac]["quality"]<50]
                            w=self.workers[name]
                            for s in sorted(eligible,key=lambda s:recent[s.client_mac]["quality"])[:3]:
                                key=(name,s.client_mac)
                                if self.wall()-self.client_stats_last.get(key,-1e20)<60:continue
                                try:w.submit_detail("auto-stats-"+str(s.at)+"-"+s.client_mac,"show ap debug client-stats",s.client_mac,ttl=120)
                                except (RuntimeError,ValueError):continue
                                self.client_stats_last[key]=self.wall()
                        elif reading.command=="show ap debug client-stats":
                            a=claims.get(reading.target)
                            if a:
                                from .client import match_bss
                                rr=[r for r in radios if r.controller==name and r.ap_name==a["ap_name"]]
                                rid=match_bss(a["bssid"],a["ssid"],bss.data,rr)
                                r=next((x for x in rr if x.radio_id==rid),None)
                                if r:
                                    ctx=json.dumps([sid+"|"+r.epoch,rid,a["bssid"],a["ssid"]],separators=(",",":"))
                                    self.client_history.station(reading.target,reading.observed_at,ctx,reading.data)
                        del self.detail_pending[name]
                    radio_map={r.radio_id:r for r in radios}
                    self.client_stats_last={k:v for k,v in self.client_stats_last.items() if self.wall()-v<=3600}
                    def current_client(c):
                        a=clients.get(c["client_mac"]);r=radio_map.get(c["radio_id"])
                        w=self.workers.get(c["controller"])
                        return bool(a and r and w and r.controller==c["controller"]
                            and a["ap_name"]==c["ap_name"] and a["ssid"]==c["ssid"]
                            and a["bssid"]==c.get("bssid")
                            and c.get("epoch")==w.instance_id+":"+str(w.generation)+"|"+r.epoch)
                    self.active_detail_clients=[c for c in self.client_history.latest(self.wall()) if current_client(c)]
                except (sqlite3.Error,OSError,ValueError,TypeError) as exc:
                    self.error=type(exc).__name__
                    self.active_detail_clients=[]
            self.field_quality={}
            if self.quality_ledger and not self.error and not self.simulation:
                try:self.field_quality=self.quality_ledger.evaluate(radios,active,int(self.wall()),self.accepted)
                except (sqlite3.Error,OSError,ValueError,TypeError) as exc:self.error=type(exc).__name__
            if self.live_clients:
                try:
                    now_wall=int(self.wall())
                    if not self.error and not self.paused:
                        for name,(reading,snap,bss,assoc) in list(self.live_details.items()):
                            self.live_clients.ingest(reading,name,snap,assoc,bss,radios,self.field_quality,clients,now_wall)
                        self.live_details.clear()
                        self.live_clients.plan(radios,active,self.field_quality,now_wall,self.workers)
                        self.live_client_views=self.live_clients.publish(radios,active,self.field_quality,clients,now_wall)
                        self.live_clients.plan_station(clients,now_wall,self.workers)
                    else:
                        self.live_details.clear();self.live_clients.clear();self.live_client_views=[]
                except (ValueError,TypeError,KeyError,RuntimeError,sqlite3.Error,OSError) as exc:
                    self.live_details.clear();self.live_clients.clear();self.live_client_views=[]
                    self.error=type(exc).__name__
            if self.cluster_monitor:
                self.cluster_monitor.choose(self.cluster_frames,int(self.wall()),self.inflight)
                self.cluster_view=self.cluster_monitor.report(self.cluster_frames,int(self.wall()))
            self.published_radios=tuple((r.radio_id,r.observed_at) for r in radios if not self.error and self.accepted.get(r.radio_id)==r.observed_at)
            self.last_result={"mode":"demo" if self.simulation else "diagnostic","field_validated":False,
                "coherent_controllers":len(active),"expected_controllers":len(self.workers),
                "mapped_radios":len(radios),"active_clients":len(clients),
                "unresolved_radios":len(unresolved),"client_conflicts":len(conflicts),
                "storage_error":self.error,"paused":self.paused,"cluster":self.cluster_view,
                "controllers":{k:self.outcomes.get(k,{"status":"waiting"}) for k in self.workers}}
            if not self.error and not self.paused:
                for name,w in self.workers.items():
                    if name not in self.inflight and now>=self.next_tick.get(name,0):
                        self.inflight[name]=self.pool.submit(self._step,w,bool(self.cluster_monitor and name==self.cluster_monitor.representative))
                        self.next_tick[name]=now+.25
            return dict(self.last_result)
        finally:self.lock.release()
    def recover_storage(self):
        with self.lock:
            self.store.recover()
            self.error=None
    def client_views(self):
        if self.error:return []
        return list(self.active_detail_clients)
    def job_status(self,controller):
        w=self.workers[controller]
        return [asdict(j) for j in w.jobs.list(w.wall())]
    def close(self):
        with self.lock:
            if self.closed:return
            self.closed=True
        self.pool.shutdown(wait=True,cancel_futures=True)
        for w in self.workers.values():w.close()
    def run(self,stop,interval=.25,maintenance=None):
        try:
            while not stop.is_set():
                self.pulse()
                if maintenance is not None and not self.error:
                    self.last_maintenance=maintenance.tick()
                stop.wait(interval)
        finally:self.close()
