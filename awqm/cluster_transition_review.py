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
from .cluster import MEMBERSHIP


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
        # Keep a high-water mark even when an incomplete/conflicting
        # observation invalidates the comparison baseline.
        self.window_end=None

    @staticmethod
    def withheld(reason):
        return {"status":"not_evaluated","reason":reason}

    def _membership_frame(self,frames,targets,lo,hi,now):
        """Existing C03 observations only; no field validation or cause claim."""
        if not targets or set(targets)!=self.controllers or len(set(targets.values()))!=len(targets):
            return None
        reverse={v:k for k,v in targets.items()}
        times=[];states={}
        for source,address in sorted(targets.items()):
            reading=(frames.get(source) or {}).get("readings",{}).get(MEMBERSHIP)
            if not reading or reading.status!="success" or not 0<=now-reading.observed_at<=120:
                return None
            rows={r["address"]:r for r in reading.data}
            if len(rows)!=len(reading.data) or set(rows)!=set(reverse):
                return None
            for addr,row in rows.items():
                if row["type"]!=("self" if addr==address else "peer") or row["state"]=="UNKNOWN":
                    return None
                states[(source,reverse[addr])]=row["state"]
            times.append(reading.observed_at)
        if max(hi,*times)-min(lo,*times)>30:
            return None
        return (tuple(times),states)

    @staticmethod
    def _membership_comparison(previous,current):
        old,new=previous[6],current[6]
        if old is None or new is None or max(previous[1],*old[0])>=min(current[0],*new[0]):
            return {"status":"not_evaluated","reason":"complete_aligned_membership_required"}
        changes=sum(old[1][key]!=value for key,value in new[1].items())
        disagreements=sum(state!=new[1][(target,target)]
                          for (source,target),state in new[1].items() if source!=target)
        return {"status":"observed","scope":"field_unvalidated_cluster_observations",
                "prior_start":min(old[0]),"prior_end":max(old[0]),
                "current_start":min(new[0]),"current_end":max(new[0]),
                "directional_observations":len(new[1]),"changed_observations":changes,
                "current_peer_self_disagreements":disagreements}

    def observe(self,snapshots,frames,radios,quality,now,targets=None):
        def reset(reason):
            self.previous=None
            self.result=None
            return self.withheld(reason)
        if self.last_now is not None and now<self.last_now:
            # A wall-clock rollback is not a new observation epoch.
            # Preserve the temporal watermark and wait for fresh evidence.
            return reset("window_replay_or_overlap")
        self.last_now=now
        by={s.controller:s for s in snapshots}
        if len(self.controllers)<2 or len(by)!=len(snapshots) or set(by)!=self.controllers:
            return reset("complete_cluster_required")
        times=[];context=[];untils=[];gaps=[];source_times=[]
        for name,s in sorted(by.items()):
            frame=frames.get(name) or {}
            if (not frame.get("connected") or not s.field_tag or not s.field_connection
                or frame.get("_field_connection")!=s.field_connection
                or not s.field_until or now>=s.field_until
                or s.anchor_at is None or s.inventory_at is None
                or not 0<=now-s.inventory_at<=660):
                return reset("matching_attestation_required")
            times.extend((s.observed_at,s.anchor_at))
            source_times.append((name,s.observed_at,s.anchor_at))
            context.append((name,s.field_tag,s.field_connection,s.field_profile))
            untils.append(s.field_until)
            gaps.append(270 if s.field_profile=="safe" else 90)
        lo,hi=min(times),max(times)
        if not 0<=now-hi or now-lo>90 or hi-lo>30:
            return reset("time_unaligned")
        if self.blocked_through is not None and lo<=self.blocked_through:
            return reset("radio_observation_unusable")
        same_window=self.previous is not None and self.previous[:2]==(lo,hi)
        if not same_window and self.window_end is not None and lo<=self.window_end:
            # Do not promote overlapping or older observations to a baseline.
            # A partially newer window also cannot be replayed after rejection.
            self.window_end=max(self.window_end,hi)
            return reset("window_replay_or_overlap")
        self.window_end=hi if self.window_end is None else max(self.window_end,hi)
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
        membership=self._membership_frame(frames,targets,lo,hi,now)
        current=(lo,hi,context,records,min(untils),tuple(source_times),membership)
        previous=self.previous
        if previous is not None and previous[:2]==current[:2]:
            if previous[2]!=context:
                return reset("matching_attestation_required")
            if previous[5]!=current[5]:
                return reset("window_replay_or_overlap")
            if previous[3]!=records:
                self.blocked_through=hi
                return reset("radio_observation_unusable")
            if previous[6]!=membership:
                # Do not replace a same-window membership observation.
                # Keep the independently valid RF observation.
                self.previous=previous[:6]+(None,)
                if self.result:
                    self.result=dict(self.result,membership_context={
                        "status":"not_evaluated","reason":"membership_observation_changed"})
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
            "membership_context":self._membership_comparison(previous,current),
            "cause_assessment":"withheld_epoch_change",
            "service_impact":"unverified","verified":False}
        return dict(self.result)
