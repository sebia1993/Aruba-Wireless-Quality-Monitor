"""CL01 current-window review evidence, not a causal or service-impact finding.

Only current, locally attested Radio risk on distinct physical APs with an
unambiguous AAC and a matching resource-approved connection is eligible.
No new SSH, raw device identifiers, grouping or historical persistence.
A bounded volatile resource baseline is optional; it is not an RF baseline.
"""
import re
from .model import number
from .controller_resources import DATAPATH,observations

CPU="show cpuload"
AP_ID=re.compile(r"[A-Za-z0-9_.-]{1,64}:(?:[0-9a-f]{2}:){5}[0-9a-f]{2}",re.ASCII)

class ControllerLoadHistory:
    """Per-controller, bounded volatile resource observations (not an archive).

    Never cross connection/approval/CPU inventory changes or wall-clock
    rollback. No AP or Station identifiers, no persisted resource history.
    """
    MAX_AGE=1800
    MAX_PAIRS=256

    def __init__(self):
        self.clear()

    def clear(self):
        self.scope=None
        self.inventory=None
        self.last_now=None
        self.pairs={}

    def context(self,scope,now):
        if self.scope!=scope or (self.last_now is not None and now<self.last_now):
            self.clear()
        self.scope=scope
        self.last_now=now
        self.pairs={k:v for k,v in self.pairs.items()
                    if all(0<=now-t<=self.MAX_AGE for t in k)}

    def record(self,cpu_at,cpu_value,dp):
        inventory=tuple((r["id"],r["type"]) for r in dp["data"]["rows"])
        if self.inventory!=inventory:
            self.pairs.clear()
            self.inventory=inventory
        key=(cpu_at,dp["observed_at"])
        v={"cpu_at":cpu_at,"cpu_percent":cpu_value,
           "datapath_at":key[1],
           "datapath_max_1s_percent":dp["data"]["max_1s_percent"],
           "datapath_max_4s_percent":dp["data"]["max_4s_percent"]}
        if key in self.pairs and self.pairs[key] is not None and self.pairs[key]!=v:
            # Conflicting observation at the same timestamp is not a baseline.
            self.pairs.clear()
            self.pairs[key]=None
        elif key not in self.pairs:
            self.pairs[key]=v
        if len(self.pairs)>self.MAX_PAIRS:
            self.pairs.pop(min(self.pairs,key=lambda k:(max(k),k)))

    def before_risk(self,starts,radio_at):
        if not starts or any(type(t) is not int or not 0<=t<=radio_at for t in starts):
            return None
        start=min(starts)
        lo,hi=start-900,start-300
        eligible=[k for k,v in self.pairs.items() if v is not None and all(lo<=t<=hi for t in k)]
        if not eligible:return None
        # Choose the latest eligible pair, not the largest/smallest CPU.
        key=max(eligible,key=lambda k:(max(k),k))
        return dict(self.pairs[key],risk_first_at=start)


def review_controller_load(controller,frame,snapshot,radios,quality,now,history=None):
    def withheld(reason):
        return {"status":"not_evaluated","reason":reason}
    if not frame or not frame.get("connected") or not snapshot or snapshot.controller!=controller:
        if history is not None:history.clear()
        return withheld("current_connection_required")
    gate=frame.get("resource_validation") or {}
    if (not snapshot.field_tag or not snapshot.field_connection
        or frame.get("_field_connection")!=snapshot.field_connection
        or frame.get("_resource_radio_tag")!=snapshot.field_tag
        or not snapshot.field_until or not now<snapshot.field_until or not gate.get("valid")):
        if history is not None:history.clear()
        return withheld("matching_attestation_required")
    if history is not None:
        history.context((controller,snapshot.field_connection,snapshot.field_tag,
                         gate["approved_at"],tuple(gate["cpu_ids"])),now)
    dp=observations(frame,now)["datapath"]
    cpu=frame.get("readings",{}).get(CPU)
    dp_reading=frame.get("readings",{}).get(DATAPATH)
    if (dp["status"]!="observed" or cpu is None or cpu.status!="success"
        or dp_reading is None or cpu.generation!=dp_reading.generation
        or not gate["approved_at"]<=cpu.observed_at<=now<gate["until"]):
        if history is not None:history.clear()
        return withheld("resource_evidence_required")
    cpu_value=number(cpu.data,0,100)
    if cpu_value is None:
        if history is not None:history.clear()
        return withheld("resource_evidence_required")
    times=(cpu.observed_at,dp["observed_at"],snapshot.observed_at)
    if any(not 0<=now-t<=90 for t in times) or max(times)-min(times)>30:
        return withheld("time_unaligned")
    if history is not None:history.record(cpu.observed_at,cpu_value,dp)
    if cpu_value<70:return withheld("cpu_screen_not_met")
    # The CPU threshold is the existing collection-protection threshold,
    # not a vendor fault threshold. Datapath values are observations only;
    # no approved Datapath fault threshold exists.
    aps=set();radio_ids=set();normal_radios={};risk_starts=[];expires=min(snapshot.field_until,dp["valid_until"],min(times)+91)
    for r in radios:
        if (r.controller!=controller or r.source!="aos8-cli-unvalidated"
            or r.observed_at!=snapshot.observed_at or not r.valid
            or not AP_ID.fullmatch(r.ap_id) or not r.radio_id.startswith(r.ap_id+":")
            or not re.fullmatch(r"[0-9]+",r.radio_id[len(r.ap_id)+1:],re.ASCII)):
            continue
        q=quality.get(r.radio_id,{})
        if (q.get("scope")!="local_operator_attested_radio" or q.get("observed_at")!=r.observed_at
            or q.get("approval")!=snapshot.field_tag[:12] or not now<q.get("valid_until",0)):
            continue
        if (q.get("status")=="PERSISTENT" and q.get("incident_id")
            and (r.utilization>=75 or r.interference>=30 or r.noise>-80)):
            aps.add(r.ap_id);radio_ids.add(r.radio_id)
            risk_starts.append(q.get("incident_first_at"))
            expires=min(expires,q["valid_until"])
        elif (q.get("status")=="NORMAL" and not q.get("incident_id")
            and r.utilization<50 and r.interference<10 and r.noise<=-90):
            # A normal observed Radio on another AP is limited contrary
            # evidence, not proof that the entire AP or controller is healthy.
            normal_radios[r.radio_id]=(r.ap_id,q["valid_until"])
    if len(aps)<2:return withheld("distinct_persistent_aps_required")
    other_normals=[(ap,until) for ap,until in normal_radios.values() if ap not in aps]
    if other_normals:
        expires=min(expires,min(until for _,until in other_normals))
    counter={"status":"observed" if other_normals else "not_observed",
             "other_ap_count":len({ap for ap,_ in other_normals}),
             "normal_radio_count":len(other_normals)}
    baseline=history.before_risk(risk_starts,snapshot.observed_at) if history is not None else None
    return {"status":"review_candidate","code":"CL01","confidence":"low",
            "cpu_percent":cpu_value,"cpu_at":cpu.observed_at,
            "datapath_max_1s_percent":dp["data"]["max_1s_percent"],
            "datapath_max_4s_percent":dp["data"]["max_4s_percent"],
            "datapath_at":dp["observed_at"],"radio_at":snapshot.observed_at,
            "ap_count":len(aps),"radio_count":len(radio_ids),"valid_until":expires,
            "baseline":"observed" if baseline is not None else "unavailable",
            "baseline_observation":baseline,"datapath_threshold":"unvalidated",
            "counter_evidence":counter,
            "service_impact":"unverified","verified":False}
