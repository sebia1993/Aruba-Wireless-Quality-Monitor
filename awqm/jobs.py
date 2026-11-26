"""Bounded in-memory, idempotent diagnostic reservations.

A reservation is not a command result. Job state is independent of HTTP or
browser connections. Persistence across restarts is a later service task.
"""
from collections import OrderedDict
from dataclasses import dataclass, replace
from threading import RLock
import re
from uuid import uuid4
from .collector import command

DETAIL_COMMANDS={"show ap debug client-table","show ap debug client-stats","show ap arm neighbors bssid"}
TERMINAL={"completed","failed","cancelled","expired","skipped"}

@dataclass(frozen=True)
class Job:
    job_id:str
    request_id:str
    name:str
    target:str
    created:float
    deadline:float
    status:str="queued"
    reason:str=""
    started:float|None=None
    finished:float|None=None
    binding:str|None=None

class Jobs:
    def __init__(self,limit=20,history_limit=500):
        self.limit=limit
        self.history_limit=history_limit
        self.lock=RLock()
        self.records=OrderedDict()
        self.requests={}
        self.active={}
    def _expire(self,now):
        for j in list(self.records.values()):
            if j.status in ("queued","deferred") and now>=j.deadline:
                self.finish(j.job_id,"expired","deadline_elapsed",now)
    def _trim(self):
        while len(self.records)>=self.history_limit:
            key=next((k for k,j in self.records.items() if j.status in TERMINAL),None)
            if key is None:break
            self.records.pop(key)
            for req,job_id in list(self.requests.items()):
                if job_id==key:self.requests.pop(req,None)
    def _ensure_binding(self, job, binding):
        if binding is None or job.binding == binding:
            return job
        # A queued legacy/unbound job may adopt a physical binding before
        # execution. Never join a running or terminal unbound job as if it
        # had already executed for that physical AP.
        if job.binding is not None or job.status not in ("queued","deferred"):
            raise ValueError("physical target changed or unverified")
        job=replace(job,binding=binding)
        self.records[job.job_id]=job
        return job

    def submit(self,request_id,name,target,now,ttl=300,binding=None):
        if not isinstance(request_id,str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}",request_id):
            raise ValueError("invalid request ID")
        if name not in DETAIL_COMMANDS or not 1<=ttl<=600:
            raise ValueError("detail command or TTL not allowed")
        if binding is not None and (not isinstance(binding,str) or not re.fullmatch(r"[0-9a-f]{64}",binding)):
            raise ValueError("invalid AP binding")
        command(name,target)
        if name.endswith("client-stats") or name=="show ap arm neighbors bssid":target=target.lower()
        key=(name,target)
        with self.lock:
            self._expire(now)
            if request_id in self.requests:
                old=self.records[self.requests[request_id]]
                if (old.name,old.target)!=key:raise ValueError("request ID conflict")
                return self._ensure_binding(old,binding)
            existing=self.active.get(key)
            if existing:
                if len(self.requests)>=self.history_limit*4:raise RuntimeError("request index full")
                job=self._ensure_binding(self.records[existing],binding)
                self.requests[request_id]=existing
                return job
            if len(self.active)>=self.limit:raise RuntimeError("detail queue full")
            self._trim()
            if len(self.records)>=self.history_limit or len(self.requests)>=self.history_limit*4:
                raise RuntimeError("job history full")
            j=Job(uuid4().hex,request_id,name,target,now,now+ttl,binding=binding)
            self.records[j.job_id]=j;self.requests[request_id]=j.job_id;self.active[key]=j.job_id
            return j
    def get(self,job_id):
        with self.lock:return self.records.get(job_id)
    def by_request(self,request_id):
        with self.lock:return self.records.get(self.requests.get(request_id))
    def pending(self,now):
        with self.lock:
            self._expire(now)
            rows=[j for j in self.records.values() if j.status in ("queued","deferred")]
            # Manual requests precede young automatic detail work; aged work
            # gets a turn, without overriding base polls or safety limits.
            return sorted(rows,key=lambda j:(0 if now-j.created>=120 else 1 if j.request_id.startswith("manual-") else 2,j.created))
    def defer(self,job_id,reason):
        with self.lock:
            j=self.records[job_id]
            if j.status in ("queued","deferred"):
                self.records[job_id]=replace(j,status="deferred",reason=reason)
    def start(self,job_id,now):
        with self.lock:
            self._expire(now)
            j=self.records[job_id]
            if j.status not in ("queued","deferred"):return None
            if any(x.status=="running" for x in self.records.values()):return None
            j=replace(j,status="running",reason="",started=now);self.records[job_id]=j
            return j
    def finish(self,job_id,status,reason,now):
        if status not in TERMINAL:raise ValueError("not terminal")
        with self.lock:
            j=self.records[job_id]
            if j.status in TERMINAL:return j
            j=replace(j,status=status,reason=reason,finished=now)
            self.records[job_id]=j;self.active.pop((j.name,j.target),None)
            return j
    def cancel(self,job_id,now):
        with self.lock:
            j=self.records[job_id]
            if j.status in ("queued","deferred"):
                return self.finish(job_id,"cancelled","operator_cancel",now)
            return j
    def list(self,now):
        with self.lock:
            self._expire(now)
            return list(self.records.values())

    def purge_station(self,now):
        """Live Station Stats targets are needed only until execution finishes."""
        with self.lock:
            self._expire(now)
            gone={key for key,j in self.records.items() if j.name in ("show ap debug client-stats","show ap arm neighbors bssid") and j.status in TERMINAL}
            for key in gone:self.records.pop(key,None)
            self.requests={k:v for k,v in self.requests.items() if v not in gone}
