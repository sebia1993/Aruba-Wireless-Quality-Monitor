"""Independent worker orchestration and single-writer collection coordinator.

This module is exercised by injected fixture sessions. No production web
listener, credentials, device access, or Windows service is started here.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from threading import Lock
import time
from .aos8 import merge_radios,merge_associations

class Engine:
    def __init__(self,workers,store,site="local-trial",clock=time.monotonic,wall=time.time):
        self.workers={w.controller:w for w in workers}
        if not 1<=len(workers)<=4 or len(self.workers)!=len(workers):
            raise ValueError("one to four unique workers required")
        if store.mode!="diagnostic":raise ValueError("separate diagnostic storage required")
        self.store=store;self.site=site;self.clock=clock;self.wall=wall
        self.pool=ThreadPoolExecutor(max_workers=len(workers),thread_name_prefix="awqm-wlc")
        self.inflight={};self.snapshots={};self.outcomes={};self.next_tick={}
        self.accepted={};self.last_result={};self.error=None
        self.paused=False;self.closed=False;self.lock=Lock()
    @staticmethod
    def _step(w):
        r=w.tick()
        return r,w.snapshot()
    def submit(self,controller,request_id,name,target,ttl=300):
        # Caller must authenticate/authorize before using this internal API.
        if self.closed:raise RuntimeError("engine closed")
        return self.workers[controller].submit_detail(request_id,name,target,ttl)
    def cancel(self,controller,job_id):
        w=self.workers[controller]
        return w.jobs.cancel(job_id,w.clock())
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
                    reading,snapshot=f.result()
                    self.snapshots[name]=snapshot
                    if reading:self.outcomes[name]={"command":reading.command,"status":reading.status,"observed_at":reading.observed_at}
                except Exception as exc:
                    self.snapshots[name]=None
                    self.outcomes[name]={"status":"worker_error","reason":type(exc).__name__}
                    # Avoid a hot failure loop.
                    self.next_tick[name]=now+15
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
            fresh=[r for r in radios if r.observed_at>self.accepted.get(r.radio_id,-1)]
            if not self.error:
                try:
                    for i in range(0,len(fresh),1000):
                        batch=fresh[i:i+1000]
                        self.store.save_batch(batch)
                        self.accepted.update({r.radio_id:r.observed_at for r in batch})
                except Exception as exc:
                    self.error=type(exc).__name__
            self.last_result={"mode":"diagnostic","field_validated":False,
                "coherent_controllers":len(active),"expected_controllers":len(self.workers),
                "mapped_radios":len(radios),"active_clients":len(clients),
                "unresolved_radios":len(unresolved),"client_conflicts":len(conflicts),
                "storage_error":self.error,"paused":self.paused,
                "controllers":{k:self.outcomes.get(k,{"status":"waiting"}) for k in self.workers}}
            if not self.error and not self.paused:
                for name,w in self.workers.items():
                    if name not in self.inflight and now>=self.next_tick.get(name,0):
                        self.inflight[name]=self.pool.submit(self._step,w)
                        self.next_tick[name]=now+.25
            return dict(self.last_result)
        finally:self.lock.release()
    def recover_storage(self):
        with self.lock:
            self.store.recover()
            self.error=None
    def job_status(self,controller):
        w=self.workers[controller]
        return [asdict(j) for j in w.jobs.list(w.clock())]
    def close(self):
        with self.lock:
            if self.closed:return
            self.closed=True
        self.pool.shutdown(wait=True,cancel_futures=True)
        for w in self.workers.values():w.close()
    def run(self,stop,interval=.25):
        try:
            while not stop.is_set():
                self.pulse()
                stop.wait(interval)
        finally:self.close()
