"""Opt-in indirect retry/concurrent link-degradation evidence, never attribution.

Separate local approval permits a short-lived in-memory comparison baseline.
All inputs are locally attested successful-transmission intervals and link
observations. No raw identifier, measured occupancy, full traffic activity or
service-impact inference. A comparison baseline can itself be degraded.
"""
from collections import defaultdict,deque
from dataclasses import dataclass
import hashlib,json,time
from .service_config import safe_path
from .model import number
from .client import overlap

CHECKS=frozenset(("station_interval_alignment","same_radio_peer_comparability",
    "observed_session_continuity","baseline_privacy_15_minutes","indirect_candidate_review"))

@dataclass(frozen=True)
class CorrelationApproval:
    station_tag:str
    approved_at:int
    valid_until:int
    persist_aggregate:bool=False
    # Exact private manifest identity; never projected as client evidence.
    tag:str=""
    def permits(self,tag,at,now):
        return bool(tag and tag==self.station_tag and self.approved_at<=at<=now<self.valid_until)

def load_correlation_approvals(cfg,stations,now=None,retention_enabled=False):
    path=safe_path(cfg.root,"config/client-correlation.json")
    if not path.exists():return {}
    if path.stat().st_size>64000:raise ValueError("correlation manifest too large")
    d=json.loads(path.read_text(encoding="utf-8"))
    if d=={"version":1,"enabled":False}:return {}
    if not isinstance(d,dict) or set(d)!={"version","enabled","site","contract","controllers"} or d["version"]!=1 or d["enabled"] is not True or d["site"]!=cfg.site or d["contract"]!="aos8-indirect-retry-correlation-v1":
        raise ValueError("invalid correlation manifest")
    now=int(time.time()) if now is None else now
    rows=d["controllers"]
    if not isinstance(rows,list) or not 1<=len(rows)<=len(stations):raise ValueError("invalid correlation targets")
    result={}
    for r in rows:
        required={"controller","station_tag","approved_at","valid_until","reviewed_by","checks"}
        if not isinstance(r,dict) or not required<=set(r)<=required|{"persist_aggregate"}:
            raise ValueError("invalid correlation approval")
        persist=r.get("persist_aggregate",False)
        if type(persist) is not bool or persist and not retention_enabled:
            raise ValueError("explicit aggregate retention authorization required")
        s=stations.get(r["controller"])
        if not s or not s.tag or r["controller"] in result or r["station_tag"]!=s.tag:
            raise ValueError("exact Station Stats attestation required")
        if type(r["approved_at"]) is not int or type(r["valid_until"]) is not int or not s.approved_at<=r["approved_at"]<=now or not r["approved_at"]<r["valid_until"]<=s.valid_until:
            raise ValueError("invalid correlation lifetime")
        if not isinstance(r["reviewed_by"],str) or not 1<=len(r["reviewed_by"])<=128 or any(ord(c)<32 for c in r["reviewed_by"]):
            raise ValueError("reviewer reference required")
        checks=CHECKS|({"aggregate_evidence_retention_approval"} if persist else set())
        if not isinstance(r["checks"],dict) or set(r["checks"])!=checks or any(v is not True for v in r["checks"].values()):
            raise ValueError("all correlation checks required")
        tag=hashlib.sha256(json.dumps({"site":cfg.site,"contract":d["contract"],"record":r},
            sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        result[r["controller"]]=CorrelationApproval(s.tag,r["approved_at"],r["valid_until"],persist,tag)
    return result

class ClientCorrelation:
    def __init__(self,approvals,max_clients=5000):
        self.approvals=dict(approvals);self.max_clients=max_clients;self.history={}
        self._candidate_approvals={}
    def forget(self,ident):
        self.history.pop(ident,None)
        self._candidate_approvals.pop(ident,None)
    def clear(self):
        self.history.clear()
        self._candidate_approvals.clear()
    def may_persist(self,ident,approval):
        # A candidate evaluated without retention consent cannot acquire it
        # retrospectively, nor can an old view outlive an approval change.
        return bool(approval and approval.persist_aggregate
                    and self._candidate_approvals.get(ident)==approval)
    def evaluate(self,observations,now):
        """Input is a bounded mapping of ephemeral identifiers to internal
        link/station context, not raw MACs. Retain only currently visible,
        approved session sequences, never across a missing observation.
        """
        self._candidate_approvals.clear()
        observations={k:v for k,v in observations.items()
            if v["controller"] in self.approvals
            and all(self.approvals[v["controller"]].permits(v["station_tag"],at,now)
                    for at in (v["start"],v["end"],v["link_at"],v["radio_at"]))
            and 0<=now-v["end"]<=90 and now<v["until"]}
        if len(observations)>self.max_clients:self.clear();return {}
        def context(item):
            # Station continuity alone is not continuity of consent.
            # An independently renewed/reviewed correlation manifest starts
            # a new private baseline, even with unchanged Station tag.
            return (item["sequence"],item["controller"],item["radio_id"],item["radio_epoch"],
                    item["network"],item["station_tag"],self.approvals[item["controller"]])
        for ident in tuple(self.history):
            item=observations.get(ident)
            saved=self.history[ident]
            if not item or saved[0]!=context(item):
                self.forget(ident);continue
            # Expire retained points even when the latest link observation
            # is unaligned or no successful-Tx increase was measured.
            hist=saved[1]
            while hist and (not 0<=now-hist[0]["end"]<=900 or now>=hist[0]["until"]):hist.popleft()
            if not hist:self.forget(ident)
        groups=defaultdict(list)
        for ident,x in observations.items():
            # Only >=100 successful Tx frames (retry ratio available) with a
            # temporally aligned link and Radio observation is comparable.
            if (not 30<=x["end"]-x["start"]<=180 or x["activity"]!="successful_tx_observed"
                or number(x["retry"],0,100) is None or number(x["health"],0,100) is None
                or number(x["score"],0,100) is None or abs(x["link_at"]-x["end"])>30
                or abs(x["radio_at"]-x["end"])>30 or x["persistent"] is not True):
                continue
            entry=self.history.get(ident)
            if entry is None:
                entry=(context(x),deque(maxlen=24));self.history[ident]=entry
            hist=entry[1]
            while hist and (now-hist[0]["end"]>900 or now>=hist[0]["until"]):hist.popleft()
            if hist and (x["end"]<hist[-1]["end"] or now<hist[-1]["end"]):
                self.forget(ident);continue
            if not hist or x["end"]>hist[-1]["end"]:
                hist.append({k:x[k] for k in ("start","end","health","retry","until")})
            groups[(x["controller"],x["radio_id"],x["radio_epoch"],x["network"])].append((ident,x))
        result={}
        for rows in groups.values():
            # Do not silently select a favorable subset of an oversized cohort.
            if len(rows)>32:continue
            for ident,x in rows:
                if x["score"]>=50 or x["retry"]<25 or x["utilization"]<75:continue
                peers=[]
                for other,y in rows:
                    if other==ident or abs(x["end"]-y["end"])>30 or overlap(x["start"],x["end"],y["start"],y["end"])<.8:
                        continue
                    baseline=[b for b in self.history[other][1]
                              if 300<=y["end"]-b["end"]<=900 and b["end"]<=y["start"]]
                    if not baseline:continue
                    # Most recent eligible baseline, not the best or worst one.
                    base=baseline[-1]
                    degraded=base["health"]-y["health"]>=20 or y["retry"]-base["retry"]>=15
                    peers.append((degraded,min(y["until"],base["until"],y["end"]+90,base["end"]+901),base))
                degraded=sum(d for d,_,_ in peers)
                if len(peers)>=2 and degraded>=2:
                    # A common current interval alone is insufficient:
                    # require one common earlier window for the comparable
                    # cohort, not a favorable subset or hand-picked sample.
                    starts=[b["start"] for _,_,b in peers]
                    ends=[b["end"] for _,_,b in peers]
                    longest=max(e-s for s,e in zip(starts,ends))
                    if max(ends)-min(ends)>30 or (min(ends)-max(starts))*5<longest*4:
                        continue
                    result[ident]={"candidate":"RF03","confidence":"low","peer_comparable":len(peers),
                        "peer_degraded":degraded,"candidate_until":min(x["until"],x["end"]+90,*(t for _,t,_ in peers)),
                        "candidate_scope":"indirect_successful_retry_peer_degradation"}
                    self._candidate_approvals[ident]=self.approvals[x["controller"]]
        return result
