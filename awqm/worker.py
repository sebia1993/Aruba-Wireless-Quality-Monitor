"""Independent controller worker, dependency-injected for safe simulation.

No live connection is created by the current launcher. Session factories are
provided by tests; a future approved service will inject the pinned SSH session.
Only commands with a parser are enabled in this increment.
"""
from dataclasses import dataclass
from threading import Lock
import re
import time
from uuid import uuid4
from .aos8 import (radio_summary,inventory,active_details,associations,cpu_load,
                   ControllerSnapshot,ParseError,merge_radios,merge_associations)
from .scheduler import Scheduler,DEFAULT_POLLS,Poll
from .jobs import Jobs
from .client import client_table,station_stats,bss_table
from .field_validation import version_identity,fingerprint
from .station_validation import collect as collect_station,StationUnavailable
from .cluster import MEMBERSHIP,AP_LOAD,CLIENT_LOAD,LOAD_COMMANDS,group_membership,ap_load,client_load
from .live_neighbors import COMMAND as NEIGHBOR,width_value
from .rf_neighbors import arm_neighbors
from .association_history import COMMANDS as USER_TABLES,user_table
from .manual_jobs import bind_ap
from .controller_resources import COMMANDS as RESOURCE_COMMANDS,MEMORY,DATAPATH,memory,datapath
from .ap_anchors import COMMANDS as ANCHOR_COMMANDS,STANDBY,UAC,standby,uac_database
from .cli_capabilities import matrix as capability_matrix

PARSERS={
    "show cpuload":cpu_load,
    "show version":version_identity,
    "show ap radio-summary":radio_summary,
    "show ap active details":active_details,
    "show ap database long":inventory,
    "show ap association":associations,
    "show ap bss-table":bss_table,
    MEMBERSHIP:group_membership,AP_LOAD:ap_load,CLIENT_LOAD:client_load,
    **{cmd:user_table for cmd in USER_TABLES},
    MEMORY:memory,DATAPATH:datapath,STANDBY:standby,UAC:uac_database,
}
DETAIL_PARSERS={"show ap debug client-table":client_table,"show ap debug client-stats":station_stats,NEIGHBOR:arm_neighbors}
DENIED=re.compile(r"(?im)^\s*(?:%\s*)?(?:permission denied|access denied|not authorized|authorization failed)")
UNSUPPORTED=re.compile(r"(?im)^\s*(?:%\s*)?(?:invalid (?:input|command)|unknown command|command not found|unrecognized command|ambiguous command|incomplete command)")
AUTH_ERRORS={"AuthenticationException","BadAuthenticationType","PasswordRequiredException","BadHostKeyException","HostKeyUnapproved","IncompatiblePeer","SharedCredentialRejected","CredentialUnavailable"}
BACKOFF=(15,30,60,120,300)

@dataclass(frozen=True)
class Reading:
    command: str
    status: str
    observed_at: int
    data: object=None
    reason: str=""
    generation: int=0
    target: str|None=None

class ControllerWorker:
    def __init__(self,controller,session_factory,clock=time.monotonic,wall=time.time,mode="safe",offset=0,enable_bss=False,field_approval=None,live_detail=False,client_approval=None,station_approval=None,enable_cluster=False,neighbor_approval=None,history_approval=None,manual_detail=False,resource_approval=None,anchor_approval=None):
        self.controller=controller;self.session_factory=session_factory
        self.clock=clock;self.wall=wall
        self.field_approval=field_approval;self.enable_cluster=enable_cluster;self.neighbor_approval=neighbor_approval;self.history_approval=history_approval;self.manual_detail=bool(manual_detail);self.resource_approval=resource_approval;self.anchor_approval=anchor_approval
        self.live_detail=live_detail;self.client_approval=client_approval;self.station_approval=station_approval
        polls=tuple(p for p in DEFAULT_POLLS if p.name in PARSERS and p.name!="show version" and (enable_bss or p.name!="show ap bss-table") and (enable_cluster or p.name!=MEMBERSHIP) and (history_approval is not None or p.name not in USER_TABLES) and (resource_approval is not None or p.name not in RESOURCE_COMMANDS) and (enable_cluster and anchor_approval is not None or p.name not in ANCHOR_COMMANDS))
        if enable_cluster:polls=polls+(Poll(AP_LOAD,120,20,2),Poll(CLIENT_LOAD,120,20,2))
        if field_approval is not None:polls=polls+(Poll("show version",600,30,0),)
        self.schedule=Scheduler(start=clock(),offset=offset,polls=polls,mode=mode)
        self.session=None;self.next_connect=clock()+offset;self.failures=0
        self.blocked=None;self.disabled=set();self.latest={};self.outcomes={}
        self.generation=0
        self.instance_id=uuid4().hex
        self.lock=Lock()
        self.jobs=Jobs()
        self.detail_latest={}
        self.detail_disabled=set()
        self.detail_outcomes={}
        self.last_heartbeat=self.clock()
        self.maintenance_requested=False
    def _close(self):
        if self.session is not None:
            try:self.session.close()
            finally:self.session=None
    def _failed(self,reason):
        self._close()
        self.detail_latest.clear()
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
        r=Reading(name,status,int(self.wall()),data,reason,self.generation)
        self.outcomes[name]=r
        if status=="success":
            self.latest[name]=r
        return r
    def tick(self,cluster_load=False):
        """At most one command. No backlog replay or concurrent CLI."""
        if not self.lock.acquire(blocking=False):return None
        try:
            now=self.clock()
            if self.maintenance_requested or self.schedule.paused or self.blocked or now<self.next_connect:
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
                self.generation+=1
                self.schedule.reconnect(self.clock())
                for k in self.disabled:self.schedule.due[k]=float("inf")
            if self.enable_cluster:
                # Only the coordinator-selected representative may execute
                # cluster-wide AP/client load polls.
                for cmd in LOAD_COMMANDS:
                    if cmd in self.disabled or not cluster_load:self.schedule.due[cmd]=float("inf")
                    elif self.schedule.due[cmd]==float("inf"):self.schedule.due[cmd]=self.clock()
            p=self.schedule.next(self.clock())
            if p is None:
                reading=self._detail_tick()
                if reading is not None and reading.command in DETAIL_PARSERS:
                    # Do not retain AP/client targets, payloads or exception text.
                    self.detail_outcomes[reading.command]=(reading.status,reading.observed_at,reading.generation)
                return reading
            if p.name in RESOURCE_COMMANDS or p.name in ANCHOR_COMMANDS:
                at=int(self.wall());a=self.resource_approval if p.name in RESOURCE_COMMANDS else self.anchor_approval;r=self.field_approval;v=self.latest.get("show version")
                protection=self.schedule.protection
                if not (a and r and v and v.generation==self.generation and self.outcomes[v.command].status=="success"
                    and protection.effective(self.clock())=="normal" and protection.last_cpu is not None and protection.last_cpu<70
                    and r.permits(v.data,fingerprint(self.session),at,at,v.observed_at) and a.permits(r.tag,at,at)):
                    self.schedule.finish(self.clock());self.latest.pop(p.name,None)
                    return self._record(p.name,"withheld",reason="resource_authorization_or_protection" if p.name in RESOURCE_COMMANDS else "anchor_authorization_or_protection")
            if p.name in USER_TABLES:
                at=int(self.wall());a=self.history_approval;r=self.field_approval;v=self.latest.get("show version")
                if not (self.schedule.protection.effective(self.clock())=="normal" and a and r and v and v.generation==self.generation and self.outcomes[v.command].status=="success"
                    and r.permits(v.data,fingerprint(self.session),at,at,v.observed_at) and a.permits(r.tag,at,at)):
                    self.schedule.finish(self.clock());self.latest.pop(p.name,None)
                    return self._record(p.name,"withheld",reason="history_authorization_unavailable")
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
        finally:
            self.last_heartbeat=self.clock()
            try:
                if self.live_detail:self.jobs.purge_station(self.wall())
            finally:self.lock.release()
    def quiesce_collection(self,paused,offset=0):
        # Only after the coordinator drains its submitted future. An
        # in-flight web authentication also owns this lock.
        if not self.lock.acquire(blocking=False):return False
        try:
            self._close()
            self.latest.clear();self.outcomes.clear();self.detail_latest.clear();self.detail_outcomes.clear()
            now=self.clock()
            self.schedule.reconnect(now)
            self.schedule.paused=bool(paused)
            for name in self.disabled:self.schedule.due[name]=float("inf")
            if not paused:self.next_connect=max(self.next_connect,now+offset)
            for job in self.jobs.pending(self.wall()):
                self.jobs.finish(job.job_id,"skipped","maintenance_context_changed",self.wall())
            if self.live_detail:self.jobs.purge_station(self.wall())
            self.maintenance_requested=bool(paused)
            return True
        finally:self.lock.release()

    def protection_frame(self):
        if not self.lock.acquire(blocking=False):
            return {"connection":"busy","cpu":None,"protection":"unknown","profile":self.schedule.mode,"cli_capabilities":None}
        try:
            now=self.clock();p=self.schedule.protection
            return {"connection":"blocked" if self.blocked else "connected" if self.session is not None else "disconnected",
                    "block_reason":self.blocked,"cpu":p.last_cpu if p.last_at is not None and 0<=now-p.last_at<=90 else None,
                    "protection":p.effective(now),"profile":self.schedule.mode,
                    "cli_seconds_300":round(p.budget(now,300),2),"detail_seconds_60":round(p.budget(now,60,True),2),
                    "retry_after_seconds":round(max(0,self.next_connect-now),1),
                    "disabled_polls":sorted(self.disabled),"disabled_details":sorted(self.detail_disabled),
                    "cli_capabilities":capability_matrix(self),
                    "heartbeat_age_seconds":round(max(0,now-self.last_heartbeat),1)}
        finally:self.lock.release()
    def user_frame(self):
        with self.lock:
            return {cmd:r for cmd in USER_TABLES if (r:=self.outcomes.get(cmd)) and r.status=="success" and r.generation==self.generation and self.session is not None and 0<=self.wall()-r.observed_at<=330}
    def cluster_frame(self):
        if not self.enable_cluster:return None
        with self.lock:
            readings={}
            if self.session is not None:
                commands=("show cpuload",MEMBERSHIP,*LOAD_COMMANDS,*RESOURCE_COMMANDS)
                if self.anchor_approval:commands+=(*ANCHOR_COMMANDS,"show ap database long")
                for cmd in commands:
                    r=self.outcomes.get(cmd)
                    if r and r.generation==self.generation:readings[cmd]=r
            validation=None
            if self.resource_approval:
                a=self.resource_approval;r=self.field_approval;v=self.latest.get("show version");now=int(self.wall())
                valid=bool(self.session is not None and not self.blocked and not self.maintenance_requested and r and v
                    and v.generation==self.generation and self.outcomes[v.command].status=="success"
                    and r.permits(v.data,fingerprint(self.session),now,now,v.observed_at) and a.permits(r.tag,now,now))
                validation={"valid":valid,"approved_at":a.approved_at,
                    "until":min(a.valid_until,r.valid_until,v.observed_at+660) if valid else 0,"cpu_ids":a.cpu_ids}
            anchor_validation=None
            if self.anchor_approval:
                a=self.anchor_approval;r=self.field_approval;v=self.latest.get("show version");now=int(self.wall())
                valid=bool(self.session is not None and not self.blocked and not self.maintenance_requested and r and v
                    and v.generation==self.generation and self.outcomes[v.command].status=="success"
                    and r.permits(v.data,fingerprint(self.session),now,now,v.observed_at) and a.permits(r.tag,now,now))
                anchor_validation={"valid":valid,"approved_at":a.approved_at,
                    "until":min(a.valid_until,r.valid_until,v.observed_at+660) if valid else 0}
            return {"connected":self.session is not None and not self.blocked,
                    "readings":readings,"disabled":tuple(self.disabled),"resource_validation":validation,
                    "anchor_validation":anchor_validation}
    def submit_detail(self,request_id,name,target,ttl=300,binding=None):
        # Separate queue lock allows reservation even while the worker awaits SSH.
        return self.jobs.submit(request_id,name,target,self.wall(),ttl,binding=binding)
    def _detail_target(self,job):
        now=int(self.wall())
        if job.name==NEIGHBOR:
            b=self.latest.get("show ap bss-table");a=self.latest.get("show ap active details")
            if any(not x or x.generation!=self.generation or self.outcomes[x.command].status!="success" for x in (b,a)):return False
            if not 0<=now-b.observed_at<=330 or not 0<=now-a.observed_at<=150:return False
            rows=[x for x in b.data if x["bssid"]==job.target and x["service"]]
            return len(rows)==1 and sum(x["name"]==rows[0]["ap_name"] and x["ip"]==rows[0]["ip"] and "A" in x["roles"] for x in a.data)==1
        if job.name.endswith("client-table"):
            r=self.latest.get("show ap active details")
            if not r or r.generation!=self.generation or self.outcomes[r.command].status!="success" or not 0<=now-r.observed_at<=150:
                return False
            anchors=[a for a in r.data if a["name"]==job.target and "A" in a["roles"]]
            if len(anchors)!=1:return False
            if self.live_detail and job.request_id.startswith("manual-"):
                inv=self.latest.get("show ap database long")
                if not inv or inv.generation!=self.generation or self.outcomes[inv.command].status!="success" or not 0<=now-inv.observed_at<=660:return False
                entries=[a for a in inv.data if a["name"]==job.target and a["ip"]==anchors[0]["ip"]]
                if len(entries)!=1 or not job.binding:return False
                try:return bind_ap(entries[0]["mac"])==job.binding
                except ValueError:return False
            return True
        r=self.latest.get("show ap association")
        a=self.latest.get("show ap active details")
        if not r or not a or any(x.generation!=self.generation or self.outcomes[x.command].status!="success" or not 0<=now-x.observed_at<=150 for x in (r,a)):
            return False
        claims=[x for x in r.data if x["client_mac"]==job.target and x["active"]]
        return len(claims)==1 and sum(x["name"]==claims[0]["ap_name"] and "A" in x["roles"] for x in a.data)==1
    def _live_detail_allowed(self,job):
        if job.name==NEIGHBOR:
            a=self.neighbor_approval;radio=self.field_approval;now=int(self.wall())
            if not self.live_detail or not a or not a.automatic or not radio:return False
            readings=[self.latest.get(k) for k in ("show version","show ap radio-summary","show ap bss-table")]
            if any(not x or x.generation!=self.generation or self.outcomes[x.command].status!="success" for x in readings):return False
            v,r,b=readings
            if not (0<=now-r.observed_at<=90 and 0<=now-b.observed_at<=330):return False
            if not radio.permits(v.data,fingerprint(self.session),r.observed_at,now,v.observed_at) or not a.permits(radio.tag,r.observed_at,now):return False
            target=[x for x in b.data if x["bssid"]==job.target and x["service"]]
            if len(target)!=1:return False
            t=target[0];width=width_value(t["width"])
            matches=[x for x in r.data if x["name"]==t["ap_name"] and x["ip"]==t["ip"] and x["band"]==t["band"] and x["channel"]==t["channel"] and x["service"]]
            if len(matches)!=1 or width is None:return False
            x=matches[0]
            return bool(a.block(x["band"],x["mode"].rsplit(":",1)[-1],width) and
                       all(x[k] is not None for k in ("utilization","interference","noise")) and
                       (x["utilization"]>=75 or x["interference"]>=30 or x["noise"]>-80))
        if not self.live_detail:return True
        # Service workers explicitly opt in to the live gate. A separate
        # client approval, current version/key, and recent association/BSS
        # and RF observations are required before a CLI is sent.
        a=self.client_approval;radio=self.field_approval
        now=int(self.wall())
        manual=self.manual_detail and job.name=="show ap debug client-table" and job.request_id.startswith("manual-")
        if job.request_id.startswith("manual-") and not manual:return False
        if not a or not (a.automatic or manual) or not radio or job.name not in ("show ap debug client-table","show ap debug client-stats"):return False
        keys=("show version","show ap radio-summary","show ap association","show ap bss-table")
        readings=[self.latest.get(k) for k in keys]
        if any(not x or x.generation!=self.generation or self.outcomes[x.command].status!="success" for x in readings):return False
        v,r,assoc,bss=readings
        if not (0<=now-r.observed_at<=90 and 0<=now-assoc.observed_at<=90 and 0<=now-bss.observed_at<=330):return False
        if not radio.permits(v.data,fingerprint(self.session),r.observed_at,now,v.observed_at):return False
        if not a.permits(radio.tag,r.observed_at,now):return False
        target=job.target
        if job.name=="show ap debug client-stats":
            st=self.station_approval
            if not st or not st.permits(a.tag,r.observed_at,now):return False
            claims=[x for x in assoc.data if x["active"] and x["client_mac"]==job.target]
            if len(claims)!=1:return False
            target=claims[0]["ap_name"]
        return any(x["name"]==target and x["service"] and x["utilization"] is not None and x["utilization"]>=75 for x in r.data)
    def _detail_tick(self):
        now=self.clock()
        for job in self.jobs.pending(self.wall()):
            if job.name in self.detail_disabled:
                self.jobs.finish(job.job_id,"skipped","command_unavailable",self.wall());continue
            if not self._live_detail_allowed(job):
                self.jobs.defer(job.job_id,"neighbor_validation_or_freshness_required" if job.name==NEIGHBOR else "client_validation_or_freshness_required");continue
            if not self._detail_target(job):
                self.jobs.defer(job.job_id,"target_anchor_unverified");continue
            poll=Poll(job.name,60,10,3,True)
            available=min(10-self.schedule.protection.budget(now,60,True),150-self.schedule.protection.budget(now,300))
            if not self.schedule.protection.allowed(poll,now) or available<1:
                self.jobs.defer(job.job_id,"controller_protection");continue
            if not self.jobs.start(job.job_id,self.wall()):continue
            try:
                if self.live_detail and job.name=="show ap debug client-stats":
                    try:
                        value=collect_station(self.session,job.target,self.clock,self.wall,min(10,available))
                    except StationUnavailable:
                        self.detail_disabled.add(job.name)
                        self.jobs.finish(job.job_id,"skipped","station_or_association_unavailable",self.wall())
                        return Reading(job.name,"unsupported",int(self.wall()),generation=self.generation,target=job.target)
                    except (ValueError,TypeError):
                        self.jobs.finish(job.job_id,"failed","station_validation_failed",self.wall())
                        return Reading(job.name,"parse_error",int(self.wall()),generation=self.generation,target=job.target)
                else:
                    raw=self.session.run(job.name,target=job.target,timeout=min(10,available),idle_timeout=min(10,available))
                    if DENIED.search(raw) or UNSUPPORTED.search(raw):
                        self.detail_disabled.add(job.name)
                        self.jobs.finish(job.job_id,"skipped","permission_denied_or_unsupported",self.wall())
                        return Reading(job.name,"unsupported",int(self.wall()),generation=self.generation,target=job.target if self.live_detail and job.name in (NEIGHBOR,"show ap debug client-table") else None)
                    try:
                        value=DETAIL_PARSERS[job.name](raw)
                    except (ValueError,TypeError):
                        self.jobs.finish(job.job_id,"failed","parse_error",self.wall())
                        return Reading(job.name,"parse_error",int(self.wall()),generation=self.generation,target=job.target if self.live_detail and job.name in (NEIGHBOR,"show ap debug client-table") else None)
                reading=Reading(job.name,"success",int(self.wall()),value,generation=self.generation,target=job.target)
                # Bounded cache; full historical client persistence is not yet enabled.
                key=(job.name,job.target)
                if not self.live_detail:
                    self.detail_latest.pop(key,None);self.detail_latest[key]=reading
                    while len(self.detail_latest)>100:self.detail_latest.pop(next(iter(self.detail_latest)))
                self.jobs.finish(job.job_id,"completed","field_validation_pending",self.wall())
                return reading
            except Exception as exc:
                self.jobs.finish(job.job_id,"failed",type(exc).__name__,self.wall())
                self._failed(type(exc).__name__)
                return Reading(job.name,"transport_error",int(self.wall()),generation=self.generation,target=job.target if self.live_detail and job.name in ("show ap debug client-stats","show ap debug client-table",NEIGHBOR) else None)
            finally:
                self.schedule.protection.account(now,self.clock(),True)
        return None
    def close(self):
        with self.lock:
            self._close()
    def snapshot(self,max_skew=45):
        """Each command has its own freshness contract; anchor identity is rechecked.
        The Radio measurement timestamp is not changed to the inventory timestamp.
        A slow inventory poll must not suppress every subsequent Radio sample.
        """
        with self.lock:
            keys=("show ap database long","show ap active details","show ap radio-summary")
            readings=[self.latest.get(k) for k in keys]
            if self.session is None or any(r is None or r.generation!=self.generation for r in readings):return None
            if any(self.outcomes[k].status!="success" for k in keys):return None
            times=[r.observed_at for r in readings]
            now=int(self.wall())
            radio_t=times[2]
            # Inventory and physical anchor metadata have slower approved poll periods.
            # Never use future data or a failed refresh as authoritative current data.
            ages=[now-t for t in times]
            # Safe mode intentionally polls radio/association more slowly.
            radio_limit=360 if self.schedule.mode=="safe" else max_skew+45
            limits=(660,150,radio_limit)
            if any(age<0 or age>limit for age,limit in zip(ages,limits)):return None
            if abs(times[0]-radio_t)>660 or abs(times[1]-radio_t)>150:return None
            a=self.latest.get("show ap association")
            assoc=a.data if a and a.generation==self.generation and self.outcomes[a.command].status=="success" and 0<=now-a.observed_at<=150 and abs(a.observed_at-radio_t)<=max_skew else None
            approval=self.field_approval;tag=None;until=None
            v=self.latest.get("show version")
            if (approval and v and v.generation==self.generation and self.outcomes[v.command].status=="success"
                and approval.permits(v.data,fingerprint(self.session),radio_t,now,v.observed_at)):
                tag=approval.tag;until=min(approval.valid_until,v.observed_at+660)
            return ControllerSnapshot(self.controller,radio_t,readings[0].data,readings[1].data,readings[2].data,assoc,
                inventory_at=times[0],anchor_at=times[1],association_at=a.observed_at if assoc is not None else None,
                field_tag=tag,field_until=until,field_connection=self.instance_id+":"+str(self.generation),field_profile=self.schedule.mode)

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
