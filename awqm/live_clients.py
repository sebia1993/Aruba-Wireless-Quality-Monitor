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
from .client_correlation import ClientCorrelation

CHECKS=frozenset(("client_table_layout","active_association_mapping","bss_radio_mapping",
    "client_health_percent","ack_snr_db","last_phy_rate_mbps","privacy_approval","read_only_permission"))

@dataclass(frozen=True)
class ClientApproval:
    radio_tag:str
    approved_at:int
    valid_until:int
    automatic:bool
    tag:str=""
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
        tag=hashlib.sha256(json.dumps({"site":cfg.site,"contract":"aos8-client-table-v1","record":r},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        out[r["controller"]]=ClientApproval(a.tag,r["approved_at"],r["valid_until"],r["automatic"],tag)
    return out

class LiveClients:
    def __init__(self,approvals,key=None,max_rows=5000,station_approvals=None,correlation_approvals=None):
        self.approvals=dict(approvals);self.key=key or secrets.token_bytes(32)
        self.selector=Selector(cooldown=180,allow_attested=True)
        self.max_rows=max_rows;self.rows={};self.views=[];self.last_plan={}
        self.station_approvals=dict(station_approvals or {});self.station={};self.station_plan={}
        self.correlation=ClientCorrelation(correlation_approvals or {},max_rows)
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
    def _forget_station(self,ident):
        self.station.pop(ident,None);self.correlation.forget(ident)
    def invalidate_ap(self,controller,target):
        removed=[k for k,r in self.rows.items() if r["controller"]==controller and r["ap_name"]==target]
        for ident in removed:
            self.rows.pop(ident,None);self._forget_station(ident)
    def ingest(self,reading,controller,snapshot,assoc,bss,radios,quality,clients,now):
        if reading.command=="show ap debug client-stats":
            result=self.ingest_station(reading,controller,snapshot,assoc,bss,radios,quality,clients,now)
            if not result and reading.target:self._forget_station(self.token("client",reading.target))
            return result
        if reading.command!="show ap debug client-table" or not reading.target:return 0
        def invalid():
            self.invalidate_ap(controller,reading.target);return 0
        if (reading.status!="success" or not snapshot or not assoc or not bss or
            reading.generation!=assoc.generation or reading.generation!=bss.generation or
            not 0<=now-reading.observed_at<=90 or not 0<=reading.observed_at-assoc.observed_at<=90 or
            not 0<=reading.observed_at-bss.observed_at<=330):
            return invalid()
        if not isinstance(reading.data,list) or len(reading.data)>2000:return invalid()
        a=self.approvals.get(controller)
        if not a or not snapshot.field_tag or now>=(snapshot.field_until or 0) or not a.permits(snapshot.field_tag,reading.observed_at,now):return invalid()
        relevant=[r for r in radios if r.controller==controller and r.ap_name==reading.target]
        if not relevant or len({r.ap_id for r in relevant})!=1:return invalid()
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
                context=snapshot.field_connection,radio_epoch=r.epoch,network=self.token("network",d["ssid"]),association=self.token("association",json.dumps([d["bssid"],d["ssid"]])))))
        # No raw MAC, BSSID or SSID is retained by the client ledger.
        # A new detail observation replaces this AP's previous view. Missing
        # entries are not interpreted as disconnection or proof of recovery.
        previous={k:v for k,v in self.rows.items() if v["controller"]==controller and v["ap_name"]==reading.target}
        self.rows={k:v for k,v in self.rows.items() if k not in previous}
        for ident,row in new:
            old=previous.pop(ident,None)
            if old and (old["context"],old["association"],old["radio_id"],old["radio_epoch"])!=(row["context"],row["association"],row["radio_id"],row["radio_epoch"]):
                self._forget_station(ident)
            self.rows[ident]=row
        for ident in previous:self._forget_station(ident)
        if len(self.rows)>self.max_rows:
            keep=sorted(self.rows,key=lambda k:self.rows[k]["observed_at"],reverse=True)[:self.max_rows]
            self.rows={k:self.rows[k] for k in keep}
        return len(new)
    def publish(self,radios,snapshots,quality,clients,now):
        by={s.controller:s for s in snapshots}
        rs={r.radio_id:r for r in radios}
        claims={self.token("client",m):v for m,v in clients.items()}
        views=[];comparison={}
        self.rows={k:r for k,r in self.rows.items() if 0<=now-r["observed_at"]<=180 and now<r["valid_until"]}
        for ident,row in self.rows.items():
            s=by.get(row["controller"]);r=rs.get(row["radio_id"]);a=claims.get(ident)
            if not s or not r or not a or s.field_connection!=row["context"] or r.epoch!=row["radio_epoch"] or not self.eligible(r,s,quality.get(r.radio_id),now):continue
            if a["ap_name"]!=row["ap_name"] or self.token("association",json.dumps([a["bssid"],a["ssid"]]))!=row["association"]:continue
            # API consumers receive only the explicit display allowlist.
            item={k:row[k] for k in ("id","ap_name","controller","band","observed_at","health","snr",
                "last_phy_rate","quality_score","quality_status","airtime","retry","candidate","scope","valid_until")}
            station=self.station.get(ident);approval=self.station_approvals.get(row["controller"])
            item.update(station_retry=None,station_start=None,station_end=None,station_tx_activity="unknown",station_until=None)
            if station and approval and 0<=now-station["at"]<=90 and now<station["until"] and station["context"]==row["context"] and station["association"]==row["association"] and station["radio_epoch"]==row["radio_epoch"]:
                item.update(station_retry=station["ratio"],station_start=station["start"],station_end=station["at"],
                            station_tx_activity=station["activity"],station_until=station["until"])
                ca=self.correlation.approvals.get(row["controller"])
                if ca and station["start"] is not None:
                    comparison[ident]={"controller":row["controller"],"radio_id":row["radio_id"],"radio_epoch":r.epoch,
                        "network":row["network"],"station_tag":approval.tag,"sequence":station["sequence"],
                        "start":station["start"],"end":station["at"],"activity":station["activity"],
                        "retry":station["ratio"],"health":row["health"],"score":row["quality_score"],
                        "link_at":row["observed_at"],"radio_at":r.observed_at,"persistent":True,
                        "utilization":r.utilization,"until":min(row["valid_until"],station["until"],s.field_until,ca.valid_until)}
            views.append(item)
        # Remove stale/context-invalid station observations. No raw identifiers or
        # station counters are ever written to disk by this component.
        visible={v["id"] for v in views}
        self.station={k:v for k,v in self.station.items() if k in visible and 0<=now-v["at"]<=180 and now<v["until"]}
        self.station_plan={k:v for k,v in self.station_plan.items() if 0<=now-v<=180}
        candidates=self.correlation.evaluate(comparison,now)
        for row in views:
            row.update(candidates.get(row["id"],{"candidate":None,"confidence":"unavailable",
                "peer_comparable":None,"peer_degraded":None,"candidate_until":None,"candidate_scope":None}))
        self.views=sorted(views,key=lambda r:(r["quality_score"] is None,r["quality_score"] or 0,r["id"]))
        return self.views
    def clear(self):
        self.rows.clear();self.station.clear();self.station_plan.clear();self.correlation.clear();self.views=[]

    def ingest_station(self,reading,controller,snapshot,assoc,bss,radios,quality,clients,now):
        from .station_validation import continuous,successful_retry
        if reading.status!="success" or not reading.target or not snapshot or not assoc or not bss or reading.generation!=assoc.generation or reading.generation!=bss.generation:
            return 0
        if not 0<=now-reading.observed_at<=30 or not 0<=reading.observed_at-assoc.observed_at<=90 or not 0<=reading.observed_at-bss.observed_at<=330:
            return 0
        approval=self.station_approvals.get(controller);client=self.approvals.get(controller)
        if not approval or not client or not approval.permits(client.tag,reading.observed_at,now):return 0
        ident=self.token("client",reading.target or "")
        row=self.rows.get(ident);claim=clients.get(reading.target)
        if not row or not claim or row["controller"]!=controller or row["context"]!=snapshot.field_connection:return 0
        local=[x for x in assoc.data if x["active"] and x["client_mac"]==reading.target]
        if len(local)!=1 or any(local[0][k]!=claim[k] for k in ("ap_name","bssid","ssid")):return 0
        r=next((x for x in radios if x.radio_id==row["radio_id"] and x.controller==controller),None)
        if not r or r.epoch!=row["radio_epoch"] or not self.eligible(r,snapshot,quality.get(r.radio_id),now):return 0
        if match_bss(claim["bssid"],claim["ssid"],bss.data,[r])!=r.radio_id:return 0
        before,after=reading.data["before"],reading.data["after"]
        association=self.token("association",json.dumps([claim["bssid"],claim["ssid"]]))
        if ((after.ap,after.bssid,after.ssid)!=(claim["ap_name"],claim["bssid"],claim["ssid"]) or
            association!=row["association"] or row["ap_name"]!=after.ap or not continuous(before,after,10)):
            self._forget_station(ident);return 0
        # Store only a pseudonymized stamp and temporary counters, no BSSID/SSID.
        stamp_token=self.token("station-association",json.dumps([after.ap,after.bssid,after.ssid,after.aid,after.count]))
        current=reading.data["counters"]
        if any(type(current[k]) is not int or not 0<=current[k]<=2**63-1 for k in ("sent","retried")) or current["retried"]>current["sent"]:
            self._forget_station(ident);return 0
        old=self.station.get(ident);ratio=None;start=None;activity="unknown";sequence=secrets.token_hex(16)
        if old and reading.observed_at<=old["at"]:return 0
        if old and old["context"]==row["context"] and old["stamp"]==stamp_token and old["association"]==association and old["radio_epoch"]==row["radio_epoch"]:
            dt=reading.observed_at-old["at"]
            if 30<=dt<=180 and after.age>=old["age"] and abs(after.age-old["age"]-dt)<=3:
                sent=current["sent"]-old["counters"]["sent"]
                retried=current["retried"]-old["counters"]["retried"]
                if 0<=retried<=sent:
                    sequence=old["sequence"]
                    start=old["at"]
                    activity="successful_tx_observed" if sent>0 else "no_successful_tx_increase"
                    ratio=successful_retry(old["counters"],current,stamp_token,stamp_token)
        if old and sequence!=old["sequence"]:self.correlation.forget(ident)
        self.station[ident]={"sequence":sequence,"radio_epoch":row["radio_epoch"],"at":reading.observed_at,"start":start,"ratio":ratio,"activity":activity,
            "until":min(row["valid_until"],approval.valid_until,snapshot.field_until),"context":row["context"],
            "association":association,"stamp":stamp_token,"age":after.age,"counters":{"sent":current["sent"],"retried":current["retried"]}}
        return 1

    def plan_station(self,clients,now,workers):
        # Keep existing 3-target controller cap and 45s cooldown. When the
        # extra comparison approval exists, prefer a same-Radio/network
        # cohort (one poor link + two deterministic peers) rather than
        # selecting only poor links across unrelated APs.
        candidates={};cohorts={}
        claims={self.token("client",m):m for m in clients}
        for row in self.views:
            ident=row["id"];controller=row["controller"]
            approval=self.station_approvals.get(controller);a=self.approvals.get(controller)
            if not approval or not a or not approval.permits(a.tag,row["observed_at"],now):continue
            if row["quality_score"] is None or ident not in claims:continue
            internal=self.rows.get(ident)
            if internal is None:continue
            ca=self.correlation.approvals.get(controller)
            if ca and ca.permits(approval.tag,row["observed_at"],now):
                group=(controller,internal["radio_id"],internal["radio_epoch"],internal["network"],internal["context"])
                cohorts.setdefault(group,[]).append(row)
            if row["quality_score"]<50:candidates.setdefault(controller,[]).append(row)
        planned=[]
        for controller,poor in candidates.items():
            w=workers.get(controller)
            if not w or "show ap debug client-stats" in getattr(w,"detail_disabled",set()):continue
            if hasattr(w.jobs,"purge_station"):w.jobs.purge_station(now)
            active=[j for j in w.jobs.list(now) if j.name=="show ap debug client-stats" and j.status in ("queued","deferred","running")]
            slots=max(0,3-len(active))
            if not slots:continue
            # Choose a deterministic, bounded same-network cohort if one
            # exists. Peer selection is independent of its current retry.
            ranked=sorted(poor,key=lambda x:(x["quality_score"],x["id"]))
            selected=None
            for target in ranked:
                x=self.rows[target["id"]]
                group=cohorts.get((controller,x["radio_id"],x["radio_epoch"],x["network"],x["context"]),[])
                peers=sorted((r for r in group if r["id"]!=target["id"]),key=lambda r:r["id"])
                if len(peers)>=2:
                    selected=[target,*peers[:2]];break
            if selected is None:selected=ranked
            for row in selected:
                if not slots:break
                ident=row["id"];target=claims[ident]
                if now-self.station_plan.get(ident,-1e20)<45 or any(j.target==target for j in active):continue
                try:
                    j=w.submit_detail("station-"+ident+"-"+str(now//45),"show ap debug client-stats",target,ttl=60)
                except (RuntimeError,ValueError):continue
                self.station_plan[ident]=now;planned.append(j.job_id);slots-=1
        return planned
