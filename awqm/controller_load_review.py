"""CL01 current-window review evidence, not a causal or service-impact finding.

Only current, locally attested Radio risk on distinct physical APs with an
unambiguous AAC and a matching resource-approved connection is eligible.
No new SSH, public raw device identifiers, grouping or historical persistence.
Bounded volatile resource and matched Radio comparison evidence is optional.
"""
import hashlib,json,re
from .model import number
from .controller_resources import DATAPATH,observations

CPU="show cpuload"
AP_ID=re.compile(r"[A-Za-z0-9_.-]{1,64}:(?:[0-9a-f]{2}:){5}[0-9a-f]{2}",re.ASCII)

class ControllerLoadHistory:
    """Per-controller, bounded volatile resource observations (not an archive).

    Never cross connection/approval/CPU inventory changes or wall-clock
    rollback. Radio identities are private in bounded process memory only;
    no Station identities, raw identifiers in output, or persisted history.
    """
    MAX_AGE=1800
    MAX_PAIRS=256
    MAX_RADIO_FRAMES=32
    MAX_RADIOS_PER_FRAME=4096

    def __init__(self):
        self.clear()

    def clear(self):
        self.scope=None
        self.inventory=None
        self.last_now=None
        self.pairs={}
        self.blocked_through=None
        self.radio_frames={}
        self.radio_blocked_through=None
        self.recovery=None

    def context(self,scope,now):
        if self.scope!=scope or (self.last_now is not None and now<self.last_now):
            self.clear()
        self.scope=scope
        self.last_now=now
        if self.recovery and now-self.recovery['risk']['radio_at']>self.MAX_AGE:
            self.recovery=None
        self.pairs={k:v for k,v in self.pairs.items()
                    if all(0<=now-t<=self.MAX_AGE for t in k)}
        self.radio_frames={t:v for t,v in self.radio_frames.items()
                           if 0<=now-t<=self.MAX_AGE}

    def record(self,cpu_at,cpu_value,dp):
        inventory=tuple((r["id"],r["type"]) for r in dp["data"]["rows"])
        if self.inventory!=inventory:
            self.pairs.clear()
            self.radio_frames.clear()
            self.inventory=inventory
            self.recovery=None
        key=(cpu_at,dp["observed_at"])
        if self.blocked_through is not None and min(key)<=self.blocked_through:
            return False
        # A single resource reading can be reused with a newer reading of
        # the other resource. Detect contradictions by each source timestamp,
        # not just by the compound pair key. Keep only a private digest of
        # the approved per-CPU fields; identical maxima can hide a conflict.
        fingerprint=hashlib.sha256(json.dumps(dp["data"]["rows"],
            sort_keys=True,separators=(",",":")).encode("utf-8")).hexdigest()
        v={"cpu_at":cpu_at,"cpu_percent":cpu_value,
           "datapath_at":key[1],
           "datapath_max_1s_percent":dp["data"]["max_1s_percent"],
           "datapath_max_4s_percent":dp["data"]["max_4s_percent"],
           "_datapath_fingerprint":fingerprint}
        for previous_key,previous in self.pairs.items():
            cpu_conflict=previous_key[0]==key[0] and previous["cpu_percent"]!=cpu_value
            dp_conflict=previous_key[1]==key[1] and any(previous[k]!=v[k] for k in
                ("_datapath_fingerprint","datapath_max_1s_percent","datapath_max_4s_percent"))
            if cpu_conflict or dp_conflict:
                # Do not cherry-pick unaffected-looking old pairs or accept
                # a replay of either conflicting reading. Start a fresh
                # comparison segment only when both readings are newer.
                self.blocked_through=max(max(key),max(max(k) for k in self.pairs))
                self.pairs.clear()
                self.radio_frames.clear()
                self.recovery=None
                return False
        self.pairs[key]=v
        if len(self.pairs)>self.MAX_PAIRS:
            self.pairs.pop(min(self.pairs,key=lambda k:(max(k),k)))
        return True

    def record_radio_snapshot(self,at,records):
        """Only valid, committed, attested same-snapshot observations.
        
        Reject conflicting replays instead of joining two partial views.
        A frame that exceeds the bounded capacity is not sampled or truncated.
        """
        if self.radio_blocked_through is not None and at<=self.radio_blocked_through:
            return False
        if records is None or len(records)>self.MAX_RADIOS_PER_FRAME or (
            at in self.radio_frames and self.radio_frames[at]!=records):
            self.radio_blocked_through=max([at,*self.radio_frames])
            self.radio_frames.clear()
            self.recovery=None
            return False
        self.radio_frames[at]=dict(records)
        if len(self.radio_frames)>self.MAX_RADIO_FRAMES:
            del self.radio_frames[min(self.radio_frames)]
        return True

    def remember_risk(self,public,records,radio_ids):
        """Keep one bounded, private cohort of the latest CL01 candidate."""
        cohort={key:(records[key][:3],records[key][9]) for key in radio_ids}
        ids=[v[1] for v in cohort.values()]
        if (any(not isinstance(i,str) or not re.fullmatch(r"[0-9a-f]{32}",i) for i in ids)
            or len(set(ids))!=len(ids)):
            self.recovery=None
            return
        at=public["radio_at"]
        if self.recovery and at<self.recovery["last_at"]:
            self.recovery=None
            return
        risk={k:public[k] for k in ("cpu_percent","cpu_at","datapath_at",
            "datapath_max_1s_percent","datapath_max_4s_percent","radio_at","ap_count","radio_count")}
        self.recovery={"cohort":cohort,"risk":risk,"last_at":at,"resolved_at":None}

    def after_recovery(self,records,quality,resource,at,expires,max_gap):
        """Compare only an explicitly resolved, unchanged physical cohort.

        This initial slice requires every contributing Incident to resolve
        in the same committed Radio frame. Missing/staggered resolution,
        new incidents, context changes or observation gaps invalidate it.
        It is an observation within five minutes, not a five-minute soak,
        a parent Incident, or proof of controller/service recovery.
        """
        state=self.recovery
        if state is None:return None
        def discard():
            self.recovery=None
            return None
        if at<state["last_at"] or at-state["last_at"]>max_gap:
            return discard()
        cohort=state["cohort"]
        if any(key not in records or records[key][:3]!=ctx for key,(ctx,_) in cohort.items()):
            return discard()
        rows=[(records[k],ident) for k,(_,ident) in cohort.items()]
        if state["resolved_at"] is None:
            if any(row[9]!=ident for row,ident in rows):return discard()
            statuses={row[8] for row,_ in rows}
            if statuses=={"RESOLVED"}:
                if at<=state["risk"]["radio_at"]:return discard()
                state["resolved_at"]=at
            elif not statuses<= {"PERSISTENT","RECOVERING"}:
                return discard()
            state["last_at"]=at
            return None
        anchor=state["resolved_at"]
        if at==anchor:
            # Repeated coordinator publication of the exact resolution
            # frame is neither a new sample nor a post-resolution reading.
            if any(row[8]!="RESOLVED" or row[9]!=ident for row,ident in rows):
                return discard()
            return None
        if not 0<at-anchor<=300:return discard()
        if any(row[8] not in ("NORMAL","WATCH") or row[9] is not None
               or not (row[5]<65 and row[6]<20 and row[7]<=-85) for row,_ in rows):
            return discard()
        state["last_at"]=at
        # Both resource observations must actually follow the resolution,
        # not just be cached values close to the subsequent Radio sample.
        if min(resource["cpu_at"],resource["datapath_at"])<=anchor:return None
        until=min(expires,anchor+301,*(quality[k]["valid_until"] for k in cohort))
        return {"status":"recovery_observation","code":"CL01","resolved_at":anchor,
                **resource,"radio_at":at,"ap_count":state["risk"]["ap_count"],
                "radio_count":len(cohort),"risk_observation":dict(state["risk"]),
                "valid_until":until,"service_impact":"unverified","verified":False}

    def before_risk_radio(self,baseline,current):
        """Compare the exact current cohort against one aligned prior frame.

        Do not choose a favorable older Radio frame if the latest aligned
        frame lacks any contributor or has a changed physical context.
        """
        if baseline is None or not current:return None
        lo,hi=baseline["risk_first_at"]-900,baseline["risk_first_at"]-300
        resource_times=(baseline["cpu_at"],baseline["datapath_at"])
        eligible=[t for t in self.radio_frames if lo<=t<=hi
                  and max(t,*resource_times)-min(t,*resource_times)<=30]
        if not eligible:return None
        at=max(eligible)
        prior=self.radio_frames[at]
        if any(key not in prior or prior[key][:3]!=value[:3]
               for key,value in current.items()):
            return None
        # Tuples contain epoch, band, channel, observed positive risk and
        # policy-normal flag. Only aggregate counts cross the view boundary.
        observations=[prior[key] for key in current]
        return {"radio_at":at,"radio_count":len(current),
                "prior_positive_risk_radio_count":sum(row[3] for row in observations),
                "prior_policy_normal_radio_count":sum(row[4] for row in observations),
                "current_positive_risk_radio_count":sum(row[3] for row in current.values())}

    def before_risk(self,starts,radio_at):
        if not starts or any(type(t) is not int or not 0<=t<=radio_at for t in starts):
            return None
        start=min(starts)
        lo,hi=start-900,start-300
        eligible=[k for k in self.pairs if all(lo<=t<=hi for t in k)]
        if not eligible:return None
        # Choose the latest eligible pair, not the largest/smallest CPU.
        key=max(eligible,key=lambda k:(max(k),k))
        public={k:v for k,v in self.pairs[key].items() if k!="_datapath_fingerprint"}
        return dict(public,risk_first_at=start)


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
    if history is not None:
        history.context((controller,snapshot.field_connection,snapshot.field_tag,
                         snapshot.field_until,gate["approved_at"],gate["until"],
                         tuple(gate["cpu_ids"]),cpu.generation),now)
    times=(cpu.observed_at,dp["observed_at"],snapshot.observed_at)
    if any(not 0<=now-t<=90 for t in times) or max(times)-min(times)>30:
        return withheld("time_unaligned")
    if history is not None and not history.record(cpu.observed_at,cpu_value,dp):
        return withheld("resource_observation_conflict")
    # The CPU threshold is the existing collection-protection threshold,
    # not a vendor fault threshold. Datapath values are observations only;
    # no approved Datapath fault threshold exists.
    aps=set();radio_ids=set();normal_radios={};risk_starts=[];expires=min(snapshot.field_until,dp["valid_until"],min(times)+91)
    rf_records={};rf_conflict=False
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
        positive=r.utilization>=75 or r.interference>=30 or r.noise>-80
        normal=(q.get("status")=="NORMAL" and not q.get("incident_id")
                and r.utilization<50 and r.interference<10 and r.noise<=-90)
        if q.get("status") in (
            "NORMAL","WATCH","DETECTED","PERSISTENT","RECOVERING","RESOLVED"):
            # Exact RF inputs matter even when two values fall in the same
            # risk bucket. Retain them privately; the comparison API still
            # publishes only aggregate counts.
            record=(r.epoch,r.band,r.channel,positive,normal,
                    r.utilization,r.interference,r.noise,q["status"],q.get("incident_id"))
            if r.radio_id in rf_records:
                if rf_records[r.radio_id]!=record:rf_conflict=True
                # An identical duplicate contributes only once.
                continue
            # Bound the temporary map even if no history is supplied.
            if len(rf_records)<ControllerLoadHistory.MAX_RADIOS_PER_FRAME:
                rf_records[r.radio_id]=record
            else:
                rf_conflict=True
                continue
        if q.get("status")=="PERSISTENT" and q.get("incident_id") and positive:
            aps.add(r.ap_id);radio_ids.add(r.radio_id)
            risk_starts.append(q.get("incident_first_at"))
            expires=min(expires,q["valid_until"])
        elif normal:
            # A normal observed Radio on another AP is limited contrary
            # evidence, not proof that the entire AP or controller is healthy.
            normal_radios[r.radio_id]=(r.ap_id,q["valid_until"])
    if history is not None:
        rf_recorded=history.record_radio_snapshot(snapshot.observed_at,None if rf_conflict else rf_records)
        if not rf_recorded:
            return withheld("radio_observation_unusable")
    else:
        rf_recorded=False
        if rf_conflict:
            return withheld("radio_observation_unusable")
    resource={"cpu_percent":cpu_value,"cpu_at":cpu.observed_at,
              "datapath_max_1s_percent":dp["data"]["max_1s_percent"],
              "datapath_max_4s_percent":dp["data"]["max_4s_percent"],
              "datapath_at":dp["observed_at"]}
    if cpu_value<70 or len(aps)<2:
        if history is not None:
            recovery=history.after_recovery(rf_records,quality,resource,snapshot.observed_at,
                expires,270 if snapshot.field_profile=="safe" else 90)
            if recovery is not None and now<recovery["valid_until"]:return recovery
        return withheld("cpu_screen_not_met" if cpu_value<70 else "distinct_persistent_aps_required")
    other_normals=[(ap,until) for ap,until in normal_radios.values() if ap not in aps]
    if other_normals:
        expires=min(expires,min(until for _,until in other_normals))
    counter={"status":"observed" if other_normals else "not_observed",
             "other_ap_count":len({ap for ap,_ in other_normals}),
             "normal_radio_count":len(other_normals)}
    baseline=history.before_risk(risk_starts,snapshot.observed_at) if history is not None else None
    rf_baseline=None
    if rf_recorded and all(key in rf_records for key in radio_ids):
        rf_baseline=history.before_risk_radio(baseline,{key:rf_records[key] for key in radio_ids})
    result={"status":"review_candidate","code":"CL01","confidence":"low",
            **resource,"radio_at":snapshot.observed_at,
            "ap_count":len(aps),"radio_count":len(radio_ids),"valid_until":expires,
            "baseline":"observed" if baseline is not None else "unavailable",
            "baseline_observation":baseline,
            "rf_baseline":"observed" if rf_baseline is not None else "unavailable",
            "rf_baseline_observation":rf_baseline,"datapath_threshold":"unvalidated",
            "counter_evidence":counter,
            "service_impact":"unverified","verified":False}
    if history is not None:history.remember_risk(result,rf_records,radio_ids)
    return result
