"""Independent worker orchestration and single-writer collection coordinator.

This module is exercised by injected fixture sessions. No production web
listener, credentials, device access, or Windows service is started here.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict,replace
from threading import Lock
import time
import sqlite3
import shutil
from .storage import disk_level
from .aos8 import merge_radios,merge_association_tables
from .durable_jobs import DurableJobs
from .selection import Selector
from .client_history import ClientHistory,map_samples
from .live_neighbors import COMMAND as NEIGHBOR
from .ap_anchors import reconcile as reconcile_anchors
from .controller_load_review import review_controller_load
from .selected_history import SelectedHistory
import json

class Engine:
    def __init__(self,workers,store,site="local-trial",clock=time.monotonic,wall=time.time,simulation=False,durable_jobs=False,auto=False,quality_ledger=None,live_clients=None,cluster_monitor=None,live_neighbors=None,association_history=None,selected_history=False):
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
        if simulation and selected_history:raise ValueError("selected history requires diagnostic mode")
        self.selected_history=SelectedHistory(store) if selected_history else None
        self.selected_pending=self.selected_history.pending() if self.selected_history else {}
        # At most one immutable accepted receipt per in-flight controller.
        # Workers publish to this bounded mailbox; only the coordinator writes SQLite.
        self.selected_handoff={}
        self.selected_handoff_lock=Lock()
        if self.selected_history:
            if not set(self.selected_pending).issubset(self.workers):
                raise ValueError("pending selected receipt controller unavailable")
            for w in workers:w.retain_selected_history=True
        self.client_history=ClientHistory(store.root/"client_history.db",simulation=True) if simulation else None
        self.detail_pending={}
        self.client_stats_last={}
        self.active_detail_clients=[]
        self.pool=ThreadPoolExecutor(max_workers=len(workers),thread_name_prefix="awqm-wlc")
        self.inflight={};self.snapshots={};self.outcomes={};self.next_tick={}
        self.inventory_associations={}
        self.accepted={};self.last_result={};self.error=None
        self.paused=False;self.closed=False;self.lock=Lock()
        self.operation_requested={};self.operation_applied={};self.operation_error=None;self.operation_revision=None
        self.published_radios=()
        self.quality_ledger=quality_ledger;self.field_quality={}
        self.live_clients=live_clients
        if simulation and live_clients:raise ValueError("live client path is separate from demo")
        self.live_details={};self.live_client_views=[]
        self.cluster_monitor=cluster_monitor;self.cluster_frames={};self.cluster_view=None;self.anchor_views={}
        if simulation and live_neighbors:raise ValueError("live neighbor path is separate from demo")
        self.live_neighbors=live_neighbors;self.neighbor_frames={};self.neighbor_pending={};self.neighbor_views={};self.neighbor_refresh_at=None
        if simulation and association_history:raise ValueError("live history requires diagnostic mode")
        self.association_history=association_history;self.association_frames={};self.association_refresh_at=None
        # Scope peer observations to this engine/site; no cross-lock or SSH.
        if not simulation:
            for w in workers:
                w.identity_peers=tuple(p for p in workers if p is not w)
    @staticmethod
    def _step(w,cluster_load=False,receipt_sink=None):
        r=w.tick(cluster_load=cluster_load) if w.enable_cluster else w.tick()
        if receipt_sink is not None and r is not None and r.selected_receipt is not None:
            # Transfer before snapshot/cluster/user-frame construction. A later
            # failure or slow frame must not discard the accepted historical receipt.
            receipt_sink(w.controller,r.selected_receipt)
            r=replace(r,selected_receipt=None)
        snapshot=w.snapshot()
        b=w.latest.get("show ap bss-table")
        a=w.latest.get("show ap association")
        # Only a successfully refreshed, same-generation metadata observation.
        if not b or b.generation!=w.generation or w.outcomes[b.command].status!="success" or not 0<=w.wall()-b.observed_at<=330:b=None
        if not a or a.generation!=w.generation or w.outcomes[a.command].status!="success" or not 0<=w.wall()-a.observed_at<=150:a=None
        return r,snapshot,b,a,w.cluster_frame(),w.user_frame()
    def _offer_selected(self,name,receipt):
        """Worker-side bounded memory handoff; no I/O or coordinator lock."""
        if not self.selected_history or name not in self.workers or receipt.controller!=name:
            raise ValueError("invalid selected receipt handoff")
        with self.selected_handoff_lock:
            previous=self.selected_handoff.get(name)
            if previous is not None and previous!=receipt:
                raise RuntimeError("conflicting selected receipt handoff")
            self.selected_handoff[name]=receipt

    def _take_selected_handoff(self):
        """Coordinator owns pending and disk writes; preserve conflicting slots."""
        with self.selected_handoff_lock:
            for name,receipt in list(self.selected_handoff.items()):
                previous=self.selected_pending.get(name)
                if previous is not None and previous!=receipt:
                    self.error="selected_history_conflict"
                    continue
                self.selected_pending[name]=receipt
                del self.selected_handoff[name]

    def submit(self,controller,request_id,name,target,ttl=300):
        # Caller must authenticate/authorize before using this internal API.
        if self.closed:raise RuntimeError("engine closed")
        return self.workers[controller].submit_detail(request_id,name,target,ttl)
    def cancel(self,controller,job_id):
        w=self.workers[controller]
        return w.jobs.cancel(job_id,w.wall())
    def apply_operations(self,desired,error=None):
        with self.lock:
            if error:
                self.operation_error=error;self.operation_revision=None
                for name,w in self.workers.items():
                    self.operation_requested[name]={"requested_paused":True,"revision":None,"changed_at":None}
                    w.maintenance_requested=True
                return
            if set(desired["controllers"])!=set(self.workers):raise ValueError("operation scope mismatch")
            self.operation_revision=desired["revision"];self.operation_error=None
            for name,state in desired["controllers"].items():
                previous=self.operation_requested.get(name)
                if previous==state:continue
                self.operation_requested[name]=dict(state)
                if previous is None and state["revision"]==0 and not state["requested_paused"]:
                    self.operation_applied[name]=dict(state)
                    continue
                self.workers[name].maintenance_requested=True

    def _maintenance_step(self):
        blocked=set()
        for index,(name,w) in enumerate(self.workers.items()):
            wanted=self.operation_requested.get(name)
            if wanted is None:continue
            changed=self.operation_applied.get(name)!=wanted
            if wanted["requested_paused"] or changed:blocked.add(name)
            if changed:
                # In-flight results from a previous PM context cannot
                # reappear as current observations.
                for cache in (self.snapshots,self.inventory_associations,self.cluster_frames,self.neighbor_frames,self.association_frames,
                              self.detail_pending,self.live_details,self.neighbor_pending):
                    cache.pop(name,None)
                if self.live_clients:self.live_clients.clear()
                if self.live_neighbors:self.live_neighbors.clear()
                if name in self.inflight:continue
                if w.quiesce_collection(wanted["requested_paused"],index*15):
                    self.operation_applied[name]=dict(wanted)
                    self.outcomes.pop(name,None)
                    if not wanted["requested_paused"]:
                        blocked.discard(name)
                        self.next_tick[name]=self.clock()+index*15
        return blocked

    def operation_status(self):
        states=[]
        for name,w in self.workers.items():
            wanted=self.operation_requested.get(name,{"requested_paused":False,"revision":0,"changed_at":None})
            applied=self.operation_applied.get(name)
            if applied!=wanted and name in self.operation_requested:
                phase="draining" if name in self.inflight else "applying"
            else:phase="paused" if wanted["requested_paused"] else "enabled"
            protection=w.protection_frame()
            jobs=w.jobs.list(self.wall())
            counts={k:sum(j.status==k for j in jobs) for k in ("queued","deferred","running")}
            states.append({"controller":name,**wanted,"applied_revision":applied["revision"] if applied else None,
                "phase":phase,"inflight":name in self.inflight,"jobs":counts,**protection})
        result={"revision":self.operation_revision,"error":self.operation_error,"controllers":states}
        if self.selected_history:
            with self.selected_handoff_lock:
                pending=len(set(self.selected_pending)|set(self.selected_handoff))
            result["selected_history"]={"enabled":True,"pending":pending,
                "storage_error":self.error,"field_validated":False}
        return result

    def pulse(self):
        """Nonblocking relative to SSH workers; SQLite commit is serialized here.

        Failed storage freezes new collection dispatch rather than accumulating
        an unbounded queue. An explicit successful recover resumes scheduling.
        """
        if not self.lock.acquire(blocking=False):return None
        try:
            if self.closed:raise RuntimeError("engine closed")
            now=self.clock()
            worker_storage_error=None
            for name,f in list(self.inflight.items()):
                if not f.done():continue
                del self.inflight[name]
                try:
                    reading,snapshot,bss,assoc,frame,user_frame=f.result()
                    if self.selected_history and reading and reading.selected_receipt is not None:
                        receipt=reading.selected_receipt
                        previous=self.selected_pending.get(name)
                        if previous is not None and previous != receipt:
                            self.error="selected_history_conflict"
                            raise RuntimeError("conflicting pending selected receipt")
                        self.selected_pending[name]=receipt
                    if self.association_history:self.association_frames[name]=(snapshot,bss,user_frame)
                    if frame is not None:self.cluster_frames[name]=frame
                    if self.live_neighbors:
                        self.neighbor_frames[name]=(snapshot,bss)
                        if reading and reading.command==NEIGHBOR and reading.target:
                            self.neighbor_pending[name]=(reading,snapshot)
                    self.snapshots[name]=snapshot
                    if snapshot is not None and assoc is not None:
                        self.inventory_associations[name]=assoc
                    else:
                        self.inventory_associations.pop(name,None)
                    if self.client_history and reading and reading.status=="success" and reading.target:
                        self.detail_pending[name]=(reading,snapshot,bss,assoc)
                    if self.live_clients and reading and reading.target and reading.command!=NEIGHBOR and (reading.status=="success" or reading.command in ("show ap debug client-stats","show ap debug client-table")):
                        self.live_details[name]=(reading,snapshot,bss,assoc)
                    if reading:self.outcomes[name]={"command":reading.command,"status":reading.status,"observed_at":reading.observed_at}
                except Exception as exc:
                    self.snapshots[name]=None
                    self.inventory_associations.pop(name,None)
                    self.cluster_frames.pop(name,None)
                    self.neighbor_frames.pop(name,None)
                    self.association_frames.pop(name,None)
                    self.outcomes[name]={"status":"worker_error","reason":type(exc).__name__}
                    # Avoid a hot failure loop.
                    self.next_tick[name]=now+15
                    if isinstance(exc, (sqlite3.Error,OSError)):
                        worker_storage_error=type(exc).__name__
            # Harvest even when the worker is still assembling its snapshot,
            # or that assembly failed. A future removed above has already
            # offered its receipt; no later dispatch can overtake this transfer.
            if self.selected_history:
                self._take_selected_handoff()
                if not self.error:
                    try:
                        # A fresh worker/frame failure must not prevent the first
                        # durable stage of an already accepted receipt. Do not
                        # archive, resume dispatch or retry a latched storage error.
                        if worker_storage_error:
                            self._stage_selected()
                        else:
                            self._persist_selected()
                    except (ValueError,TypeError,KeyError,RuntimeError,sqlite3.Error,OSError) as exc:
                        self.error=type(exc).__name__
            if worker_storage_error:
                self.error=self.error or worker_storage_error
            maintenance_blocked=self._maintenance_step()
            active=[]
            inventory_tables=[]
            aligned_tables=[]
            for name,s in self.snapshots.items():
                if name in maintenance_blocked:continue
                if s is None:continue
                # Never relabel an expired cached snapshot as a current reading.
                w=self.workers[name];wall=int(self.wall())
                max_radio=360 if w.schedule.mode=="safe" else 90
                if not 0<=wall-s.observed_at<=max_radio:continue
                if s.anchor_at is None or not 0<=wall-s.anchor_at<=150:continue
                if s.inventory_at is None or not 0<=wall-s.inventory_at<=660:continue
                if s.associations is not None and (s.association_at is None or not 0<=wall-s.association_at<=150):
                    s=replace(s,associations=None,association_at=None)
                active.append(s)
                # A standalone count uses the association poll's own freshness.
                # Only the worker's time-aligned observations can identify RF clients.
                assoc=self.inventory_associations.get(name)
                if (assoc is not None and assoc.generation==w.generation and not self.paused
                    and 0<=wall-assoc.observed_at<=150):
                    inventory_tables.append(assoc.data)
                    if s.associations is not None and s.association_at==assoc.observed_at:
                        aligned_tables.append(s.associations)
            # Staggered WLC polls are independently freshness-checked above.
            # Conflicting ownership remains unresolved.
            radios,unresolved=merge_radios(active,self.site,max_skew=None)
            inventory_clients,conflicts=merge_association_tables(inventory_tables)
            clients=inventory_clients
            if len(aligned_tables)!=len(inventory_tables):
                clients,_=merge_association_tables(aligned_tables)
                for client in conflicts:
                    clients.pop(client,None)
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
                        if self.quality_ledger:
                            self.live_clients.persist_correlations(self.quality_ledger.incidents,radios,active,self.field_quality,now_wall)
                        self.live_clients.plan_station(clients,now_wall,self.workers)
                    else:
                        self.live_details.clear();self.live_clients.clear();self.live_client_views=[]
                except (ValueError,TypeError,KeyError,RuntimeError,sqlite3.Error,OSError) as exc:
                    self.live_details.clear();self.live_clients.clear();self.live_client_views=[]
                    self.error=type(exc).__name__
            if self.live_neighbors:
                try:
                    now_wall=int(self.wall())
                    if self.error or self.paused:
                        self.live_neighbors.clear();self.neighbor_pending.clear();self.neighbor_views={}
                    elif self.neighbor_pending or self.neighbor_refresh_at!=now_wall:
                        index=self.live_neighbors.index(radios,active,self.neighbor_frames,now_wall)
                        for name,(reading,snap) in list(self.neighbor_pending.items()):
                            self.live_neighbors.ingest(reading,name,snap,index,self.field_quality,now_wall)
                        self.neighbor_pending.clear()
                        self.live_neighbors.plan(index,self.field_quality,now_wall,self.workers)
                        self.neighbor_views=self.live_neighbors.publish(radios,active,self.field_quality,now_wall)
                        self.neighbor_refresh_at=now_wall
                except (ValueError,TypeError,KeyError,RuntimeError,sqlite3.Error,OSError) as exc:
                    self.live_neighbors.clear();self.neighbor_pending.clear();self.neighbor_views={}
                    self.error=type(exc).__name__
            if self.association_history and not self.error and not self.paused:
                now_wall=int(self.wall())
                if self.association_refresh_at!=now_wall:
                    try:self.association_history.ingest(radios,active,self.association_frames,clients,now_wall)
                    except (ValueError,TypeError,KeyError,RuntimeError,sqlite3.Error,OSError) as exc:self.error=type(exc).__name__
                    self.association_refresh_at=now_wall
            if self.cluster_monitor:
                self.cluster_monitor.choose(self.cluster_frames,int(self.wall()),self.inflight)
                self.cluster_view=self.cluster_monitor.report(self.cluster_frames,int(self.wall()))
                self.anchor_views,summaries=reconcile_anchors(radios,self.cluster_frames,int(self.wall())) if not self.error and not self.paused else ({},{})
                snapshots_by_controller={s.controller:s for s in active}
                for node in self.cluster_view['nodes']:
                    name=node['controller'];node['ap_anchors']=summaries.get(name,{})
                    if not self.error and not self.paused and not self.simulation:
                        node['load_review']=review_controller_load(name,self.cluster_frames.get(name),
                            snapshots_by_controller.get(name),radios,self.field_quality,int(self.wall()))
            self.published_radios=tuple((r.radio_id,r.observed_at) for r in radios if not self.error and self.accepted.get(r.radio_id)==r.observed_at)
            self.last_result={"mode":"demo" if self.simulation else "diagnostic","field_validated":False,
                "coherent_controllers":len(active),"expected_controllers":len(self.workers),
                "mapped_radios":len(radios),"active_clients":len(inventory_clients),
                "unresolved_radios":len(unresolved),"client_conflicts":len(conflicts),
                "storage_error":self.error,"paused":self.paused or len(maintenance_blocked)==len(self.workers),"cluster":self.cluster_view,
                "operations":self.operation_status() if self.operation_requested else None,
                "controllers":{k:self.outcomes.get(k,{"status":"waiting"}) for k in self.workers}}
            if not self.error and not self.paused:
                for name,w in self.workers.items():
                    if name not in maintenance_blocked and name not in self.inflight and now>=self.next_tick.get(name,0):
                        cluster_load=bool(self.cluster_monitor and name==self.cluster_monitor.representative)
                        if self.selected_history:
                            self.inflight[name]=self.pool.submit(self._step,w,cluster_load,self._offer_selected)
                        else:
                            self.inflight[name]=self.pool.submit(self._step,w,cluster_load)
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
    def _stage_selected(self):
        """First durable handoff; existing disk, bounds and single writer apply."""
        for receipt in self.selected_pending.values():
            space=shutil.disk_usage(self.store.root)
            if disk_level(space.free,space.total)=="critical":
                self.error="disk_protection"
                return
            self.selected_history.stage(receipt)

    def _persist_selected(self):
        """Stage bounded receipts before archive commit; retry exact IDs only."""
        self._stage_selected()
        if self.error:
            return
        for name,receipt in list(self.selected_pending.items()):
            space=shutil.disk_usage(self.store.root)
            if disk_level(space.free,space.total)=="critical":
                self.error="disk_protection"
                return
            self.selected_history.save(receipt)
            del self.selected_pending[name]

    def _drain_selected_on_close(self):
        """Archive completed accepted receipts only; never dispatch or publish.

        Called after executor shutdown, with no remaining running worker.
        A failed drain is not a clean service stop. Existing storage errors
        still require explicit recovery; never bypass critical disk protection.
        """
        incomplete = None
        worker_storage_error = None
        for name, future in list(self.inflight.items()):
            del self.inflight[name]
            if future.cancelled():
                continue
            try:
                reading = future.result()[0]
                if reading is not None and reading.selected_receipt is not None:
                    receipt = reading.selected_receipt
                    previous = self.selected_pending.get(name)
                    if previous is not None and previous != receipt:
                        self.error = "selected_history_conflict"
                        raise RuntimeError("conflicting pending selected receipt")
                    self.selected_pending[name] = receipt
            except Exception as exc:
                # Do not expose a worker exception, raw CLI or private payload.
                incomplete = type(exc).__name__
                if isinstance(exc, (sqlite3.Error, OSError)):
                    worker_storage_error = incomplete
        self._take_selected_handoff()
        if self.selected_pending and not self.error:
            try:
                # Match pulse's fail-closed behavior on fresh worker I/O failure:
                # make the first durable stage possible, but do not archive.
                if worker_storage_error:
                    self._stage_selected()
                else:
                    self._persist_selected()
            except (ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error, OSError) as exc:
                self.error = type(exc).__name__
        if worker_storage_error:
            self.error = self.error or worker_storage_error
        if incomplete is not None or self.selected_pending or self.selected_handoff:
            self.error = self.error or incomplete or "selected_history_pending"
            raise RuntimeError("selected history shutdown incomplete") from None

    def close(self):
        with self.lock:
            if self.closed:return
            self.closed=True
        self.pool.shutdown(wait=True,cancel_futures=True)
        try:
            if self.selected_history:
                self._drain_selected_on_close()
        finally:
            for w in self.workers.values():w.close()
            for w in self.workers.values():
                w.identity_peers=()
                w.retain_selected_history=False
    def run(self,stop,interval=.25,maintenance=None):
        try:
            while not stop.is_set():
                self.pulse()
                if maintenance is not None and not self.error:
                    self.last_maintenance=maintenance.tick()
                stop.wait(interval)
        finally:self.close()
