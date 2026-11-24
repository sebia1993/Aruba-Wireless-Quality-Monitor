"""Opt-in, ephemeral live Client Table investigation.

Only a separately attested Radio and client-table contract may schedule
a live AP detail read. No raw client identifiers or client history are
persisted by this component. Pseudonyms are process-local, not anonymous.
No Airtime, retry ratio, activity or root-cause claim is manufactured.
"""
from dataclasses import dataclass
import hashlib,hmac,json,secrets,time
from collections import Counter
from .service_config import safe_path
from .client import match_bss
from .analysis import client_quality
from .model import number
from .selection import Selector

CHECKS=frozenset(("client_table_layout","active_association_mapping","bss_radio_mapping",
    "client_health_percent","ack_snr_db","last_phy_rate_mbps","privacy_approval","read_only_permission"))

@dataclass(frozen=True)
class ClientApproval:
    radio_tag:str
    approved_at:int
    valid_until:int
    automatic:bool
    def permits(self,tag,at,now):
        return tag==self.radio_tag and self.approved_at<=at<=now<self.valid_until

def load_client_approvals(cfg,radio_approvals,now=None):
    p=safe_path(cfg.root,"config/client-validation.json")
    if not p.exists():return {}
    if p.stat().st_size>64000:raise ValueError("client approval too large")
    d=json.loads(p.read_text(encoding="utf-8"))
    if d=={"version":1,"enabled":False}:return {}
    if not isinstance(d,dict) or set(d)!={"version","enabled","site","contract","controllers"} or d["version"]!=1 or d["enabled"] is not True or d["site"]!=cfg.site or d["contract"]!="aos8-client-table-v1":
        raise ValueError("invalid client validation manifest")
    now=int(time.time()) if now is None else now
    rows=d["controllers"]
    if not isinstance(rows,list) or not 1<=len(rows)<=len(radio_approvals):raise ValueError("invalid client targets")
    out={}
    for r in rows:
        if not isinstance(r,dict) or set(r)!={"controller","radio_tag","approved_at","valid_until","automatic","reviewed_by","checks"}:
            raise ValueError("invalid client approval")
        a=radio_approvals.get(r["controller"])
        if not a or r["controller"] in out or r["radio_tag"]!=a.tag:
            raise ValueError("client approval must reference exact Radio approval")
        if type(r["approved_at"]) is not int or type(r["valid_until"]) is not int or not a.approved_at<=r["approved_at"]<=now or not r["approved_at"]<r["valid_until"]<=a.valid_until:
            raise ValueError("invalid client approval lifetime")
        if type(r["automatic"]) is not bool:raise ValueError("invalid automatic flag")
        if not isinstance(r["reviewed_by"],str) or not 1<=len(r["reviewed_by"])<=128 or any(ord(c)<32 for c in r["reviewed_by"]):
            raise ValueError("reviewer reference required")
        if not isinstance(r["checks"],dict) or set(r["checks"])!=CHECKS or any(v is not True for v in r["checks"].values()):
            raise ValueError("all client checks required")
        out[r["controller"]]=ClientApproval(a.tag,r["approved_at"],r["valid_until"],r["automatic"])
    return out

class LiveClients:
    def __init__(self,approvals,key=None,max_rows=5000):
        self.approvals=dict(approvals);self.key=key or secrets.token_bytes(32)
        self.selector=Selector(cooldown=180,allow_attested=True)
        self.max_rows=max_rows;self.rows={};self.views=[];self.last_plan={}
    def token(self,kind,value):
        return hmac.new(self.key,(kind+"|"+value).encode(),hashlib.sha256).hexdigest()[:32]
    def eligible(self,r,s,q,now,automatic=False):
        a=self.approvals.get(r.controller)
        return bool(a and (not automatic or a.automatic) and s and q and r.valid and
            q.get("observed_at")==r.observed_at and q.get("status")=="PERSISTENT" and
            s.field_tag and now<(s.field_until or 0) and 0<=now-r.observed_at<=90 and
            a.permits(s.field_tag,r.observed_at,now))
    def plan(self,radios,snapshots,quality,now,workers):
        by={s.controller:s for s in snapshots}
        rows=[]
        for r in radios:
            if now-self.last_plan.get(r.controller,-1e20)<60:continue
            s=by.get(r.controller);q=quality.get(r.radio_id)
            if not self.eligible(r,s,q,now,automatic=True) or r.utilization<75:continue
            rows.append(dict(source="attested-radio",status="PERSISTENT",open_incident=True,complete=r.complete,
                observed_at=r.observed_at,controller=r.controller,score=q["score"],clients=r.clients,
                ap_id=r.ap_id,ap_name=r.ap_name))
        # A selection round is no more frequent than once per minute per WLC.
        # The worker still checks the current metadata and CPU/time budget.
        planned=self.selector.plan(rows,now,workers)
        for controller,_ in planned:self.last_plan[controller]=now
        return planned
    def ingest(self,reading,controller,snapshot,assoc,bss,radios,quality,clients,now):
        if (reading.command!="show ap debug client-table" or not reading.target or
            not snapshot or not assoc or not bss or
            reading.generation!=assoc.generation or reading.generation!=bss.generation or
            not 0<=now-reading.observed_at<=90 or not 0<=reading.observed_at-assoc.observed_at<=90 or
            not 0<=reading.observed_at-bss.observed_at<=330):
            return 0
        if not isinstance(reading.data,list) or len(reading.data)>2000:return 0
        a=self.approvals.get(controller)
        if not a or not snapshot.field_tag or now>=(snapshot.field_until or 0) or not a.permits(snapshot.field_tag,reading.observed_at,now):return 0
        relevant=[r for r in radios if r.controller==controller and r.ap_name==reading.target]
        if not relevant or len({r.ap_id for r in relevant})!=1:return 0
        current={r.radio_id:r for r in relevant}
        local={}
        for d in assoc.data:
            if d["active"]:local.setdefault(d["client_mac"],[]).append(d)
        counts=Counter(d['client_mac'] for d in reading.data);new=[]
        for d in reading.data:
            mac=d["client_mac"];claim=clients.get(mac);matches=local.get(mac,[])
            if len(matches)!=1 or not claim:continue
            if not all(claim[k]==matches[0][k] for k in ("ap_name","bssid","ssid")):continue
            if claim["ap_name"]!=reading.target or claim["bssid"]!=d["bssid"] or claim["ssid"]!=d["ssid"]:continue
            rid=match_bss(d["bssid"],d["ssid"],bss.data,relevant)
            r=current.get(rid)
            if not r or abs(r.observed_at-reading.observed_at)>90 or not self.eligible(r,snapshot,quality.get(rid),now):continue
            # Duplicate client identity in different rows is ambiguous, even
            # if one row can be mapped. Do not choose whichever was last.
            if counts[mac]!=1:continue
            ident=self.token("client",mac)
            score,status=client_quality(d["health"],d["ack_snr"],None)
            new.append((ident,dict(id=ident,radio_id=rid,ap_name=r.ap_name,controller=controller,band=r.band,
                observed_at=reading.observed_at,health=d["health"],snr=d["ack_snr"],last_phy_rate=d["tx_rate"],
                quality_score=score,quality_status=status,airtime=None,retry=None,candidate=None,
                scope="locally_attested_client_table",valid_until=min(a.valid_until,snapshot.field_until),
                context=snapshot.field_connection,association=self.token("association",json.dumps([d["bssid"],d["ssid"]])))))
        # No raw MAC, BSSID or SSID is retained by the client ledger.
        # A new detail observation replaces this AP's previous view. Missing
        # entries are not interpreted as disconnection or proof of recovery.
        self.rows={k:v for k,v in self.rows.items() if not (v["controller"]==controller and v["ap_name"]==reading.target)}
        for ident,row in new:
            self.rows[ident]=row
        if len(self.rows)>self.max_rows:
            keep=sorted(self.rows,key=lambda k:self.rows[k]["observed_at"],reverse=True)[:self.max_rows]
            self.rows={k:self.rows[k] for k in keep}
        return len(new)
    def publish(self,radios,snapshots,quality,clients,now):
        by={s.controller:s for s in snapshots}
        rs={r.radio_id:r for r in radios}
        claims={self.token("client",m):v for m,v in clients.items()}
        views=[]
        self.rows={k:r for k,r in self.rows.items() if 0<=now-r["observed_at"]<=180 and now<r["valid_until"]}
        for ident,row in self.rows.items():
            s=by.get(row["controller"]);r=rs.get(row["radio_id"]);a=claims.get(ident)
            if not s or not r or not a or s.field_connection!=row["context"] or not self.eligible(r,s,quality.get(r.radio_id),now):continue
            if a["ap_name"]!=row["ap_name"] or self.token("association",json.dumps([a["bssid"],a["ssid"]]))!=row["association"]:continue
            # API consumers receive only the explicit display allowlist.
            views.append({k:row[k] for k in ("id","ap_name","controller","band","observed_at","health","snr",
                "last_phy_rate","quality_score","quality_status","airtime","retry","candidate","scope","valid_until")})
        self.views=sorted(views,key=lambda r:(r["quality_score"] is None,r["quality_score"] or 0,r["id"]))
        return self.views
    def clear(self):
        self.rows.clear();self.views=[]
