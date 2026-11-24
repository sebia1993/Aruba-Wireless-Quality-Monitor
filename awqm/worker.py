"""Independent controller worker, dependency-injected for safe simulation.

No live connection is created by the current launcher. Session factories are
provided by tests; a future approved service will inject the pinned SSH session.
Only commands with a parser are enabled in this increment.
"""
from dataclasses import dataclass
from threading import Lock
import re
import time
from .aos8 import (radio_summary,inventory,active_details,associations,cpu_load,
                   ControllerSnapshot,ParseError,merge_radios,merge_associations)
from .scheduler import Scheduler,DEFAULT_POLLS

PARSERS={
    "show cpuload":cpu_load,
    "show ap radio-summary":radio_summary,
    "show ap active details":active_details,
    "show ap database long":inventory,
    "show ap association":associations,
}
DENIED=re.compile(r"(?im)^\s*(?:%\s*)?(?:permission denied|access denied|not authorized|authorization failed)")
UNSUPPORTED=re.compile(r"(?im)^\s*(?:%\s*)?(?:invalid (?:input|command)|unknown command|command not found|unrecognized command|ambiguous command|incomplete command)")
AUTH_ERRORS={"AuthenticationException","BadAuthenticationType","PasswordRequiredException","BadHostKeyException"}
BACKOFF=(15,30,60,120,300)

@dataclass(frozen=True)
class Reading:
    command: str
    status: str
    observed_at: int
    data: object=None
    reason: str=""

class ControllerWorker:
    def __init__(self,controller,session_factory,clock=time.monotonic,wall=time.time,mode="safe",offset=0):
        self.controller=controller;self.session_factory=session_factory
        self.clock=clock;self.wall=wall
        self.schedule=Scheduler(start=clock(),offset=offset,polls=tuple(p for p in DEFAULT_POLLS if p.name in PARSERS),mode=mode)
        self.session=None;self.next_connect=clock()+offset;self.failures=0
        self.blocked=None;self.disabled=set();self.latest={};self.outcomes={}
        self.lock=Lock()
    def _close(self):
        if self.session is not None:
            try:self.session.close()
            finally:self.session=None
    def _failed(self,reason):
        self._close()
        now=self.clock();self.failures+=1
        self.next_connect=now+BACKOFF[min(self.failures-1,len(BACKOFF)-1)]
        self.schedule.reconnect(now)
        # Reconnect must not reactivate permanently denied commands.
        self.schedule.due={k:(float("inf") if k in self.disabled else v) for k,v in self.schedule.due.items()}
        return reason
    def reset_block(self):
        """Explicit administrator action only."""
        with self.lock:
            self.blocked=None;self.failures=0;self.next_connect=self.clock()
    def _record(self,name,status,data=None,reason=""):
        r=Reading(name,status,int(self.wall()),data,reason)
        self.outcomes[name]=r
        if status=="success":
            self.latest[name]=r
        return r
    def tick(self):
        """At most one command. No backlog replay or concurrent CLI."""
        if not self.lock.acquire(blocking=False):return None
        try:
            now=self.clock()
            if self.schedule.paused or self.blocked or now<self.next_connect:
                return None
            if self.session is None:
                try:
                    self.session=self.session_factory()
                    self.session.open()
                except Exception as exc:
                    label=type(exc).__name__
                    self._failed(label)
                    if label in AUTH_ERRORS:self.blocked=label
                    return self._record("connection","failed",reason=label)
                self.schedule.reconnect(self.clock())
                for k in self.disabled:self.schedule.due[k]=float("inf")
            p=self.schedule.next(self.clock())
            if p is None:return None
            try:
                raw=self.session.run(p.name,timeout=p.timeout)
            except Exception as exc:
                self.schedule.finish(self.clock())
                self._failed(type(exc).__name__)
                return self._record(p.name,"transport_error",reason=type(exc).__name__)
            try:
                if DENIED.search(raw) or UNSUPPORTED.search(raw):
                    status="permission_denied" if DENIED.search(raw) else "unsupported"
                    self.disabled.add(p.name)
                    if p.name=="show cpuload":
                        self.schedule.protection.cpu(None,self.clock())
                    r=self._record(p.name,status)
                else:
                    try:
                        result=PARSERS[p.name](raw)
                        if p.name=="show cpuload":
                            self.schedule.protection.cpu(result,self.clock())
                        r=self._record(p.name,"success",result)
                        # Only successful CLI execution resets the retry streak.
                        self.failures=0
                    except (ParseError,ValueError,TypeError):
                        if p.name=="show cpuload":
                            self.schedule.protection.cpu(None,self.clock())
                        r=self._record(p.name,"parse_error",reason="unvalidated_or_incomplete_layout")
            finally:
                self.schedule.finish(self.clock())
                if p.name in self.disabled:
                    self.schedule.due[p.name]=float("inf")
            return r
        finally:self.lock.release()
    def close(self):
        with self.lock:
            self._close()
    def snapshot(self,max_skew=45):
        """Return only a coherent snapshot, never silently relabel cached data as current."""
        with self.lock:
            keys=("show ap database long","show ap active details","show ap radio-summary")
            readings=[self.latest.get(k) for k in keys]
            if any(r is None for r in readings):return None
            if any(self.outcomes[k].status!="success" for k in keys):return None
            times=[r.observed_at for r in readings]
            if max(times)-min(times)>max_skew:return None
            if int(self.wall())-min(times)>max_skew:return None
            a=self.latest.get("show ap association")
            assoc=a.data if a and self.outcomes[a.command].status=="success" and abs(a.observed_at-times[2])<=max_skew else None
            return ControllerSnapshot(self.controller,times[2],readings[0].data,readings[1].data,readings[2].data,assoc)

class ClusterWorkers:
    def __init__(self,workers):
        self.workers=tuple(workers)
        if len(self.workers)>4 or len({w.controller for w in self.workers})!=len(self.workers):
            raise ValueError("maximum four uniquely identified controllers")
    def coherent(self,site="default"):
        snapshots=[s for w in self.workers if (s:=w.snapshot()) is not None]
        radios,unresolved=merge_radios(snapshots,site)
        clients,conflicts=merge_associations(snapshots)
        return {"radios":radios,"unresolved":unresolved,"clients":clients,
                "client_conflicts":conflicts,"coverage":len(snapshots),"expected":len(self.workers)}
