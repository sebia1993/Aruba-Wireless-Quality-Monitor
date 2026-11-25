"""Optional AP standby and UAC AP-database observations.

AOS 8 documents the tables but the public examples are empty. Only a
locally validated single-line column layout is accepted. Positive matches
are not an assurance of standby readiness or client A-UAC/S-UAC state.
"""
import json,re,time
from collections import defaultdict,Counter
from dataclasses import dataclass
from .aos8 import table,ip,mac,ParseError
from .service_config import safe_path

STANDBY="show ap standby"
UAC="show ap uac-database"
COMMANDS=(STANDBY,UAC)
AGES={STANDBY:600,UAC:600}
CONTRACT="aos8-ap-anchor-observations-v1"
CHECKS=frozenset(("standby_layout","cluster_role_semantics","uac_ap_database_layout",
    "uac_mac_matches_wired_identity","inventory_identity_mapping","read_only_permission"))

def standby(text):
    rows=table(text,"Standby AP Table",("name","ip address","cluster role"),r"(?:Num|Total) APs:\s*(\d+)")
    out=[];seen=set()
    for r in rows:
        identity=(r["name"],ip(r["ip address"]))
        roles=set(filter(None,re.split(r"[,/\s]+",r["cluster role"])))
        if not roles or not roles<={"A","SA","U","SU"} or identity in seen:
            raise ParseError("ambiguous standby identity or role")
        seen.add(identity)
        out.append({"name":identity[0],"ip":identity[1],"roles":sorted(roles)})
        if len(out)>10000:raise ParseError("standby table exceeds bound")
    return out

def uac_database(text):
    rows=table(text,"UAC AP Database",("ap name","mac address","ip address","ap group","ap type","status",
        "radio 0 bssid","radio 1 bssid"),r"Total APs:\s*(\d+)")
    out=[];seen=set()
    for r in rows:
        wired=mac(r["mac address"])
        if wired in seen:raise ParseError("duplicate UAC AP identity")
        seen.add(wired)
        # Status and BSSIDs are deliberately not propagated. The MAC
        # field's wired identity semantics are a separate local check.
        out.append({"name":r["ap name"],"ip":ip(r["ip address"]),"mac":wired})
        if len(out)>10000:raise ParseError("UAC AP table exceeds bound")
    return out

@dataclass(frozen=True)
class AnchorApproval:
    radio_tag:str
    approved_at:int
    valid_until:int
    def permits(self,tag,at,now):
        return self.radio_tag==tag and self.approved_at<=at<=now<self.valid_until

def load_anchor_approvals(cfg,radios,now=None):
    p=safe_path(cfg.root,"config/ap-anchor-validation.json")
    if not p.exists():return {}
    if p.stat().st_size>64000:raise ValueError("anchor manifest too large")
    d=json.loads(p.read_text(encoding="utf-8"))
    if d=={"version":1,"enabled":False}:return {}
    if not isinstance(d,dict) or set(d)!={"version","enabled","site","contract","controllers"} or type(d["version"]) is not int or d["version"]!=1 or d["enabled"] is not True or d["site"]!=cfg.site or d["contract"]!=CONTRACT:
        raise ValueError("invalid AP anchor manifest")
    now=int(time.time()) if now is None else now
    rows=d["controllers"]
    if not isinstance(rows,list) or not 1<=len(rows)<=len(radios):raise ValueError("invalid AP anchor controllers")
    out={}
    for r in rows:
        if not isinstance(r,dict) or set(r)!={"controller","radio_tag","approved_at","valid_until","reviewed_by","checks"}:
            raise ValueError("invalid AP anchor record")
        a=radios.get(r["controller"])
        if not a or r["controller"] in out or r["radio_tag"]!=a.tag:raise ValueError("exact Radio approval required")
        if type(r["approved_at"]) is not int or type(r["valid_until"]) is not int or not a.approved_at<=r["approved_at"]<=now or not r["approved_at"]<r["valid_until"]<=a.valid_until:
            raise ValueError("invalid AP anchor lifetime")
        if not isinstance(r["reviewed_by"],str) or not 1<=len(r["reviewed_by"])<=128 or any(ord(c)<32 for c in r["reviewed_by"]):
            raise ValueError("reviewer reference required")
        if not isinstance(r["checks"],dict) or set(r["checks"])!=CHECKS or any(v is not True for v in r["checks"].values()):
            raise ValueError("all AP anchor checks required")
        out[r["controller"]]=AnchorApproval(a.tag,r["approved_at"],r["valid_until"])
    return out

def reconcile(radios,frames,now):
    """Join positive observations to unique current AAC-mapped physical APs.

    No sum of duplicated inventory rows, no negative inference from an
    empty or missing table, no raw wired MAC or IP in the output payload.
    The first result is keyed by internal Radio ID for coordinator use.
    """
    if not any(f and f.get('anchor_validation') is not None for f in frames.values()):return {},{}
    by_mac=defaultdict(set);by_ap=defaultdict(list)
    for r in radios:
        if not 0<=now-r.observed_at<=360:continue
        try:wired=mac(r.ap_id.split(":",1)[1])
        except (IndexError,ParseError):continue
        by_mac[wired].add((r.ap_id,r.ap_name,r.controller))
        by_ap[r.ap_id].append(r)
    views=defaultdict(list);summaries={}
    for controller,frame in frames.items():
        gate=frame.get("anchor_validation") if frame else None
        readings=frame.get("readings",{}) if frame else {}
        inv=readings.get("show ap database long")
        inventory=defaultdict(list);inventory_mac=Counter()
        inv_ok=bool(gate and inv and inv.status=="success" and 0<=now-inv.observed_at<=660)
        if inv_ok:
            for row in inv.data:
                inventory[(row["name"],row["ip"])].append(row["mac"])
                inventory_mac[row["mac"]]+=1
        summary={}
        for label,cmd in (("standby",STANDBY),("uac_database",UAC)):
            item={"status":"disabled","observed_at":None,"total":None,"matched":None,"unmatched":None}
            summary[label]=item
            if gate is None:continue
            item["status"]="unavailable"
            rd=readings.get(cmd)
            if rd is None:continue
            item["observed_at"]=rd.observed_at
            if not frame.get("connected") or not gate.get("valid") or not gate["approved_at"]<=rd.observed_at<=now<gate["until"]:
                item["status"]="attestation_unavailable";continue
            if rd.status!="success":item["status"]=rd.status;continue
            if not 0<=now-rd.observed_at<=AGES[cmd]:
                item["status"]="stale";continue
            if not inv_ok or inv.generation!=rd.generation or abs(inv.observed_at-rd.observed_at)>660:
                item["status"]="inventory_unavailable";continue
            matched=set();unmatched=0
            for row in rd.data:
                ids=inventory.get((row["name"],row["ip"]),())
                if len(ids)!=1 or inventory_mac[ids[0]]!=1 or label=="uac_database" and row["mac"]!=ids[0]:
                    unmatched+=1;continue
                known=by_mac.get(ids[0],set())
                if len(known)!=1:
                    unmatched+=1;continue
                ap_id,name,aac=next(iter(known))
                if name!=row["name"] or ap_id in matched:
                    unmatched+=1;continue
                matched.add(ap_id)
                roles=[x for x in row.get("roles",[]) if x in ("SA","SU","U","A")]
                obs={"source":controller,"kind":label,"roles":roles,"observed_at":rd.observed_at,
                     "valid_until":min(gate["until"],rd.observed_at+AGES[cmd]+1,inv.observed_at+661)}
                views[ap_id].append(obs)
            item.update(status="observed",total=len(rd.data),matched=len(matched),unmatched=unmatched,
                valid_until=min(gate["until"],rd.observed_at+AGES[cmd]+1,inv.observed_at+661))
        summaries[controller]=summary
    result={}
    for ap_id,items in views.items():
        for r in by_ap[ap_id]:
            result[r.radio_id]={"aac":r.controller,"observations":sorted(items,key=lambda x:(x["source"],x["kind"])),
                "scope":"positive_ap_anchor_observation_only","service_impact":"unverified"}
    return result,summaries
