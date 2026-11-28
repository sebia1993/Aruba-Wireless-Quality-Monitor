"""CL02: bounded observation of an AAC change and RF quality difference.

The existing physical Radio merger and committed, locally attested quality
ledger are the sole inputs. Two non-overlapping complete cluster windows
are required. An AAC change breaks the Radio epoch: compare separately
observed gauge states only, never counter continuity, outage, failover
cause, or a verified root cause. No new CLI, persistence or raw IDs.
"""
import re
from collections import defaultdict
from .controller_load_review import AP_ID


class ClusterTransitionReview:
    MAX_RADIOS=4096

    def __init__(self,controllers):
        self.controllers=frozenset(controllers)
        self.clear()

    def clear(self):
        self.previous=None
        self.result=None
        self.blocked_through=None
        self.last_now=None

    @staticmethod
    def withheld(reason):
        return {"status":"not_evaluated","reason":reason}

    def observe(self,snapshots,frames,radios,quality,now):
        if self.last_now is not None and now<self.last_now:
            self.clear()
        self.last_now=now
        def reset(reason):
            self.previous=None
            self.result=None
            return self.withheld(reason)
        by={s.controller:s for s in snapshots}
        if len(self.controllers)<2 or len(by)!=len(snapshots) or set(by)!=self.controllers:
            return reset("complete_cluster_required")
        times=[];context=[];untils=[];gaps=[]
        for name,s in sorted(by.items()):
            frame=frames.get(name) or {}
            if (not frame.get("connected") or not s.field_tag or not s.field_connection
                or frame.get("_field_connection")!=s.field_connection
                or not s.field_until or now>=s.field_until
                or s.anchor_at is None or s.inventory_at is None
                or not 0<=now-s.inventory_at<=660):
                return reset("matching_attestation_required")
            times.extend((s.observed_at,s.anchor_at))
            context.append((name,s.field_tag,s.field_connection,s.field_profile))
            untils.append(s.field_until)
            gaps.append(270 if s.field_profile=="safe" else 90)
        lo,hi=min(times),max(times)
        if not 0<=now-hi or now-lo>90 or hi-lo>30:
            return reset("time_unaligned")
        if self.blocked_through is not None and lo<=self.blocked_through:
            return reset("radio_observation_unusable")
        context=tuple(context)
        records={}
        for r in radios:
            s=by.get(r.controller)
            q=quality.get(r.radio_id,{})
            if (s is None or r.observed_at!=s.observed_at
                or r.source!="aos8-cli-unvalidated" or not r.valid
                or not AP_ID.fullmatch(r.ap_id) or not r.radio_id.startswith(r.ap_id+":")
                or not re.fullmatch(r"[0-9]+",r.radio_id[len(r.ap_id)+1:],re.ASCII)
                or q.get("scope")!="local_operator_attested_radio"
                or q.get("observed_at")!=r.observed_at or q.get("approval")!=s.field_tag[:12]
                or not now<q.get("valid_until",0)):
                continue
            # The merger's epoch is AAC:band:mode. Require the same
            # band/mode/channel, without claiming epoch continuity.
            prefix=r.controller+":"
            if not r.epoch.startswith(prefix):
                continue
            record=(r.ap_id,r.controller,r.band,r.channel,r.epoch[len(prefix):],
                    r.observed_at,r.utilization,r.interference,r.noise,q.get("status"))
            if r.radio_id in records and records[r.radio_id]!=record:
                self.blocked_through=hi
                return reset("radio_observation_unusable")
            records[r.radio_id]=record
            untils.append(q["valid_until"])
            if len(records)>self.MAX_RADIOS:
                self.blocked_through=hi
                return reset("radio_observation_unusable")
        current=(lo,hi,context,records,min(untils))
        previous=self.previous
        if previous is not None and previous[:2]==current[:2]:
            if previous[2]!=context:
                self.previous=current
                self.result=None
                return self.withheld("matching_attestation_required")
            if previous[3]!=records:
                self.blocked_through=hi
                return reset("radio_observation_unusable")
            if self.result and now<min(self.result["valid_until"],current[4]):
                return dict(self.result,valid_until=min(self.result["valid_until"],current[4]))
            return self.withheld("new_window_required")
        self.previous=current
        self.result=None
        if (previous is None or previous[2]!=context or now>=previous[4]
            or lo<=previous[1] or hi-previous[0]>min(gaps)):
            return self.withheld("new_window_required")
        groups=defaultdict(lambda:{"aps":set(),"radios":0})
        for rid,new in records.items():
            old=previous[3].get(rid)
            if (old is None or old[1]==new[1] or old[0]!=new[0]
                or old[2:5]!=new[2:5] or old[5]>=new[5]):
                continue
            # An explicitly normal earlier gauge and a newly observed
            # positive risk are different measurements, not a counter delta.
            if (old[9]!="NORMAL" or not (old[6]<50 and old[7]<10 and old[8]<=-90)
                or new[9] not in ("DETECTED","PERSISTENT")
                or not (new[6]>=75 or new[7]>=30 or new[8]>-80)):
                continue
            group=groups[(old[1],new[1])]
            group["aps"].add(new[0]);group["radios"]+=1
        if not groups:
            return self.withheld("no_matched_change_observed")
        self.result={"status":"transition_observation","code":"CL02",
            "prior_start":previous[0],"prior_end":previous[1],
            "current_start":lo,"current_end":hi,
            "rows":[{"from":a,"to":b,"ap_count":len(g["aps"]),"radio_count":g["radios"]}
                    for (a,b),g in sorted(groups.items())],
            "valid_until":min(previous[4],current[4],lo+91),
            "cause_assessment":"withheld_epoch_change",
            "service_impact":"unverified","verified":False}
        return dict(self.result)
