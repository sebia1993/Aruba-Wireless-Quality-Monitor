"""CL01 current-window review evidence, not a causal or service-impact finding.

Only current, locally attested Radio risk on distinct physical APs with an
unambiguous AAC and a matching resource-approved connection is eligible.
No new SSH, raw device identifiers, grouping or historical persistence.
"""
import re
from .model import number
from .controller_resources import DATAPATH,observations

CPU="show cpuload"
AP_ID=re.compile(r"[A-Za-z0-9_.-]{1,64}:(?:[0-9a-f]{2}:){5}[0-9a-f]{2}",re.ASCII)

def review_controller_load(controller,frame,snapshot,radios,quality,now):
    def withheld(reason):
        return {"status":"not_evaluated","reason":reason}
    if not frame or not frame.get("connected") or not snapshot or snapshot.controller!=controller:
        return withheld("current_connection_required")
    gate=frame.get("resource_validation") or {}
    if (not snapshot.field_tag or not snapshot.field_connection
        or frame.get("_field_connection")!=snapshot.field_connection
        or frame.get("_resource_radio_tag")!=snapshot.field_tag
        or not snapshot.field_until or not now<snapshot.field_until or not gate.get("valid")):
        return withheld("matching_attestation_required")
    dp=observations(frame,now)["datapath"]
    cpu=frame.get("readings",{}).get(CPU)
    dp_reading=frame.get("readings",{}).get(DATAPATH)
    if (dp["status"]!="observed" or cpu is None or cpu.status!="success"
        or dp_reading is None or cpu.generation!=dp_reading.generation
        or not gate["approved_at"]<=cpu.observed_at<=now<gate["until"]):
        return withheld("resource_evidence_required")
    cpu_value=number(cpu.data,0,100)
    if cpu_value is None:return withheld("resource_evidence_required")
    times=(cpu.observed_at,dp["observed_at"],snapshot.observed_at)
    if any(not 0<=now-t<=90 for t in times) or max(times)-min(times)>30:
        return withheld("time_unaligned")
    if cpu_value<70:return withheld("cpu_screen_not_met")
    # The CPU threshold is the existing collection-protection threshold,
    # not a vendor fault threshold. Datapath values are observations only;
    # no approved Datapath fault threshold or historical comparison exists.
    aps=set();radio_ids=set();normal_radios={};expires=min(snapshot.field_until,dp["valid_until"],min(times)+91)
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
    return {"status":"review_candidate","code":"CL01","confidence":"low",
            "cpu_percent":cpu_value,"cpu_at":cpu.observed_at,
            "datapath_max_1s_percent":dp["data"]["max_1s_percent"],
            "datapath_max_4s_percent":dp["data"]["max_4s_percent"],
            "datapath_at":dp["observed_at"],"radio_at":snapshot.observed_at,
            "ap_count":len(aps),"radio_count":len(radio_ids),"valid_until":expires,
            "baseline":"unavailable","datapath_threshold":"unvalidated",
            "counter_evidence":counter,
            "service_impact":"unverified","verified":False}
