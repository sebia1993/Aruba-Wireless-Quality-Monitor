"""Optional, field-attested BSSID-scoped RF neighbor investigation.

No neighbor MAC/SSID or raw CLI is persisted or published. Local approval
must verify BSSID query receiving-radio scope, timestamp and exact channel
block semantics. Neither proximity nor a matching channel proves a cause.
"""
from dataclasses import dataclass
from datetime import datetime,timezone,timedelta
import hashlib,hmac,json,secrets,time
from collections import defaultdict
from .service_config import safe_path
from .aos8 import channel_info
from .rf_neighbors import ChannelBlock,candidate,overlap

COMMAND="show ap arm neighbors bssid"
CHECKS=frozenset(("neighbor_layout","bssid_query_receiving_radio_scope","discovered_bssid_mapping",
    "channel_block_semantics","ota_update_timezone","device_clock_sync","discovery_semantics","read_only_permission"))

@dataclass(frozen=True)
class NeighborApproval:
    radio_tag:str
    approved_at:int
    valid_until:int
    automatic:bool
    timezone_minutes:int
    blocks:dict
    def permits(self,tag,at,now):
        return tag==self.radio_tag and self.approved_at<=at<=now<self.valid_until
    def block(self,band,raw,width=None):
        if not isinstance(raw,str):return None
        b=self.blocks.get((band,raw.rstrip("*")))
        if b is None or width is not None and width!=20*len(b.subchannels):return None
        return b
    def timestamp(self,raw):
        if raw is None:return None
        try:
            return int(datetime.strptime(raw,"%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone(timedelta(minutes=self.timezone_minutes))).timestamp())
        except (TypeError,ValueError,OverflowError):return None

def load_neighbor_approvals(cfg,radio_approvals,now=None):
    p=safe_path(cfg.root,"config/neighbor-validation.json")
    if not p.exists():return {}
    if p.stat().st_size>128000:raise ValueError("neighbor approval too large")
    d=json.loads(p.read_text(encoding="utf-8"))
    if d=={"version":1,"enabled":False}:return {}
    if not isinstance(d,dict) or set(d)!={"version","enabled","site","contract","controllers"} or d["version"]!=1 or d["enabled"] is not True or d["site"]!=cfg.site or d["contract"]!="aos8-bssid-neighbors-v1":
        raise ValueError("invalid neighbor validation manifest")
    now=int(time.time()) if now is None else now
    rows=d["controllers"]
    if not isinstance(rows,list) or not 1<=len(rows)<=len(radio_approvals):raise ValueError("invalid neighbor targets")
    out={}
    for r in rows:
        keys={"controller","radio_tag","approved_at","valid_until","automatic","timezone_minutes","blocks","reviewed_by","checks"}
        if not isinstance(r,dict) or set(r)!=keys:raise ValueError("invalid neighbor approval")
        a=radio_approvals.get(r["controller"])
        if not a or r["controller"] in out or r["radio_tag"]!=a.tag:raise ValueError("neighbor approval must reference exact Radio approval")
        if type(r["approved_at"]) is not int or type(r["valid_until"]) is not int or not a.approved_at<=r["approved_at"]<=now or not r["approved_at"]<r["valid_until"]<=a.valid_until:
            raise ValueError("invalid neighbor approval lifetime")
        if type(r["automatic"]) is not bool or type(r["timezone_minutes"]) is not int or not -720<=r["timezone_minutes"]<=840:
            raise ValueError("invalid neighbor configuration")
        if not isinstance(r["reviewed_by"],str) or not 1<=len(r["reviewed_by"])<=128 or any(ord(c)<32 for c in r["reviewed_by"]):
            raise ValueError("reviewer reference required")
        if not isinstance(r["checks"],dict) or set(r["checks"])!=CHECKS or any(v is not True for v in r["checks"].values()):
            raise ValueError("all neighbor checks required")
        if not isinstance(r["blocks"],list) or not 1<=len(r["blocks"])<=256:raise ValueError("explicit verified channel blocks required")
        blocks={}
        for x in r["blocks"]:
            if not isinstance(x,dict) or set(x)!={"band","raw","subchannels"} or x["band"] not in ("2.4GHz","5GHz","6GHz") or not isinstance(x["raw"],str) or not isinstance(x["subchannels"],list):
                raise ValueError("invalid channel block")
            raw=x["raw"].rstrip("*")
            ch,width=channel_info(raw)
            b=ChannelBlock(x["band"],ch,tuple(x["subchannels"]),True)
            if b.span() is None or width!=len(b.subchannels)*20 or (x["band"],raw) in blocks:
                raise ValueError("ambiguous or unsupported channel block")
            if raw.endswith("+") and b.subchannels!=(ch,ch+4) or raw.endswith("-") and b.subchannels!=(ch-4,ch):
                raise ValueError("inconsistent 40 MHz secondary channel")
            blocks[(x["band"],raw)]=b
        out[r["controller"]]=NeighborApproval(a.tag,r["approved_at"],r["valid_until"],r["automatic"],r["timezone_minutes"],blocks)
    return out

def width_value(raw):
    if raw in ("20MHz","40MHz","80MHz","160MHz"):return int(raw[:-3])
    return None

class LiveNeighbors:
    def __init__(self,approvals,incidents=None,key=None):
        self.approvals=dict(approvals);self.incidents=incidents
        self.key=key or secrets.token_bytes(32);self.rows={};self.last={};self.last_round={};self.views={}
    def token(self,bssid):
        return hmac.new(self.key,bssid.encode("ascii"),hashlib.sha256).hexdigest()[:24]
    def attested(self,r,s,now,auto=False):
        a=self.approvals.get(r.controller)
        return bool(a and (not auto or a.automatic) and s and r.valid and
            s.field_tag and now<(s.field_until or 0) and 0<=now-r.observed_at<=90 and
            a.permits(s.field_tag,r.observed_at,now))
    def eligible(self,r,s,q,now,auto=False):
        return bool(self.attested(r,s,now,auto) and q and q.get("observed_at")==r.observed_at and q.get("status")=="PERSISTENT")
    def index(self,radios,snapshots,frames,now):
        current={s.controller:s for s in snapshots}
        by=defaultdict(list)
        for r in radios:
            by[(r.controller,r.ap_name,r.band,r.channel)].append(r)
        possible=defaultdict(list)
        for name,(saved,reading) in frames.items():
            s=current.get(name)
            if not s or not saved or not reading or s.field_connection!=saved.field_connection or not 0<=now-reading.observed_at<=330:
                continue
            anchored={(a["name"],a["ip"]) for a in s.active if "A" in a["roles"]}
            for b in reading.data:
                if not b["service"] or (b["ap_name"],b["ip"]) not in anchored:continue
                matches=by[(name,b["ap_name"],b["band"],b["channel"])]
                if len(matches)!=1:continue
                r=matches[0];a=self.approvals.get(name);raw=r.epoch.rsplit(":",1)[-1]
                width=width_value(b["width"])
                block=a.block(r.band,raw,width) if a and width is not None else None
                possible[b["bssid"]].append((r,s,block))
        # A duplicate BSSID observation is only usable if the same unique
        # physical Radio/context owns it; conflicting duplicates are rejected.
        out={}
        for b,rows in possible.items():
            if len({(r.radio_id,s.field_connection,block) for r,s,block in rows})==1:out[b]=rows[0]
        return out
    def plan(self,index,quality,now,workers):
        self.last={k:v for k,v in self.last.items() if 0<=now-v<=3600}
        by=defaultdict(dict)
        for b,(r,s,block) in index.items():
            if block is None or not self.eligible(r,s,quality.get(r.radio_id),now,True):continue
            if now-self.last.get(r.radio_id,-1e20)<300 or now-self.last_round.get(r.controller,-1e20)<60:continue
            group=by[r.controller];rank=(quality[r.radio_id]["score"],-(r.clients or 0),r.radio_id,b)
            if r.ap_id not in group or rank<group[r.ap_id][0]:group[r.ap_id]=(rank,b,r)
        planned=[]
        for name,aps in by.items():
            w=workers.get(name)
            if not w or COMMAND in getattr(w,"detail_disabled",set()):continue
            active=[j for j in w.jobs.list(now) if j.name==COMMAND and j.status in ("queued","deferred","running")]
            active_ap={index[j.target][0].ap_id for j in active if j.target in index}
            remaining=min(2,max(0,5-len(active)))
            if not remaining:continue
            for _,b,r in sorted(aps.values(),key=lambda x:x[0]):
                if not remaining:break
                if r.ap_id in active_ap:continue
                try:j=w.submit_detail("neighbor-"+self.token(b)+"-"+str(now//60),COMMAND,b,ttl=120)
                except (RuntimeError,ValueError):continue
                self.last[r.radio_id]=now;self.last_round[name]=now;remaining-=1;active_ap.add(r.ap_id)
                planned.append(j.job_id)
        return planned
    def ingest(self,reading,controller,snapshot,index,quality,now):
        source=index.get(reading.target)
        if not source:return
        first,s,block=source
        if first.controller!=controller:return
        self.rows.pop(first.radio_id,None)
        if (reading.status!="success" or not snapshot or snapshot.field_connection!=s.field_connection
            or not 0<=now-reading.observed_at<=30 or block is None or not self.eligible(first,s,quality.get(first.radio_id),now)):
            return
        a=self.approvals[controller]
        values=[]
        peer_claims={}
        for n in reading.data:
            second=index.get(n["bssid"]);stamp=a.timestamp(n["last_update_raw"])
            result={"relation":"unknown","overlap_mhz":None,"candidate":None,"confidence":"unavailable",
                    "reason":"neighbor_not_mapped_or_attested"}
            name=None;target_at=None;target_id=None;target_context=None;target_until=None
            if second:
                r,other,b=second
                if r.radio_id==first.radio_id:continue
                name=r.ap_name;target_at=r.observed_at
                # A different reported raw channel suffix is contrary evidence.
                if b and self.attested(r,other,now) and n["channel_raw"].rstrip("*")==r.epoch.rsplit(":",1)[-1].rstrip("*"):
                    target_id=r.radio_id;target_context=other.field_connection
                    target_until=min(other.field_until,self.approvals[r.controller].valid_until)
                    result=candidate(first,r,block,b,n,now,neighbor_at=stamp,mapping_verified=True,field_validated=True,
                                     first_persistent=True,second_persistent=self.eligible(r,other,quality.get(r.radio_id),now))
                    q=quality.get(r.radio_id,{})
                    if (result["candidate"] in ("RF05","RF06") and q.get("incident_id")
                        and type(q.get("valid_until")) is int and now<q["valid_until"]):
                        peer_claims[r.radio_id]=(r,other.field_tag+"|"+other.field_connection+"|"+r.epoch,
                                                  q["incident_id"],min(target_until,q["valid_until"]))
            values.append({"neighbor_id":self.token(n["bssid"]),"ap_name":name,"band":n["band"],"channel":n["primary_channel"],
                           "width":n["width_candidate"],"snr":n["snr"],"discovery":n["discovery"],"neighbor_at":stamp,
                           "target_radio_at":target_at,"target_id":target_id,"target_context":target_context,"target_until":target_until,
                           **{k:result[k] for k in ("relation","overlap_mhz","candidate","confidence","reason")}})
        # Retain strongest/candidate observations only, with explicit cap.
        values.sort(key=lambda x:(x["candidate"] is None,-(x["snr"] if x["snr"] is not None else -100),x["neighbor_id"]))
        selected=values[:100]
        self.rows[first.radio_id]={"radio_at":first.observed_at,"context":s.field_connection,
            "observed_at":reading.observed_at,"until":min(a.valid_until,s.field_until),"rows":selected,"truncated":len(values)>100}
        candidates=[x for x in selected if x["candidate"] in ("RF05","RF06")][:20]
        matches=[{k:x[k] for k in ("ap_name","band","channel","width","snr","discovery","neighbor_at","target_radio_at","relation","overlap_mhz","candidate","confidence")}
                 for x in candidates]
        if matches and self.incidents:
            source_context=s.field_tag+"|"+s.field_connection+"|"+first.epoch
            self.incidents.record_neighbors(first,quality[first.radio_id],reading.observed_at,matches,
                source_context=source_context,peer_claims=[peer_claims.get(x["target_id"]) for x in candidates])
        if len(self.rows)>100:
            keep=sorted(self.rows,key=lambda k:self.rows[k]["observed_at"],reverse=True)[:100]
            self.rows={k:self.rows[k] for k in keep}
    def publish(self,radios,snapshots,quality,now):
        rs={r.radio_id:r for r in radios};by={s.controller:s for s in snapshots}
        self.rows={k:v for k,v in self.rows.items() if 0<=now-v["observed_at"]<=180 and now<v["until"]}
        self.views={}
        for rid,v in self.rows.items():
            r=rs.get(rid);s=by.get(r.controller) if r else None
            if not r or not s or s.field_connection!=v["context"] or r.observed_at!=v["radio_at"] or not self.eligible(r,s,quality.get(rid),now):continue
            shown=[]
            for item in v["rows"]:
                x=dict(item);target=rs.get(x.get("target_id"));other=by.get(target.controller) if target else None
                if x.get("target_id") and (not target or not other or target.observed_at!=x["target_radio_at"] or
                    other.field_connection!=x["target_context"] or not self.attested(target,other,now)):
                    x.update(candidate=None,confidence="unavailable",relation="unknown",overlap_mhz=None,
                             reason="target_context_changed")
                elif x["candidate"] and not self.eligible(target,other,quality.get(target.radio_id),now):
                    x.update(candidate=None,confidence="unavailable",reason="target_congestion_not_current")
                shown.append(x)
            self.views[rid]={**{k:v[k] for k in ("radio_at","context","observed_at","until","truncated")},"rows":shown}
        return self.views
    def clear(self):
        self.rows.clear();self.views={}
