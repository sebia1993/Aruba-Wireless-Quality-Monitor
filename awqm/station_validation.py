"""Separate opt-in validation for live Station Stats and association continuity.

Only documented successful-transmission counters are compared. No attempt
failure rate, actual traffic Airtime, or causal inference is produced.
"""
from dataclasses import dataclass
import json,re,time,hashlib
from .service_config import safe_path
from .aos8 import associations,ParseError
from .analysis import retry_ratio
from .model import delta

CHECKS=frozenset(("station_layout","counter_scope","successful_retry_subset",
    "association_age_seconds","association_count_continuity","counter_reset_behavior",
    "targeted_association_permission","privacy_approval"))

@dataclass(frozen=True)
class StationApproval:
    client_tag:str
    approved_at:int
    valid_until:int
    tag:str=""
    def permits(self,tag,at,now):
        return bool(tag and tag==self.client_tag and self.approved_at<=at<=now<self.valid_until)

def load_station_approvals(cfg,clients,now=None):
    path=safe_path(cfg.root,"config/station-validation.json")
    if not path.exists():return {}
    if path.stat().st_size>64000:raise ValueError("station manifest too large")
    d=json.loads(path.read_text(encoding="utf-8"))
    if d=={"version":1,"enabled":False}:return {}
    if not isinstance(d,dict) or set(d)!={"version","enabled","site","contract","controllers"} or d["version"]!=1 or d["enabled"] is not True or d["site"]!=cfg.site or d["contract"]!="aos8-station-success-retry-v1":
        raise ValueError("invalid station manifest")
    now=int(time.time()) if now is None else now
    rows=d["controllers"]
    if not isinstance(rows,list) or not 1<=len(rows)<=len(clients):raise ValueError("invalid station targets")
    out={}
    for r in rows:
        if not isinstance(r,dict) or set(r)!={"controller","client_tag","approved_at","valid_until","reviewed_by","checks"}:
            raise ValueError("invalid station approval")
        a=clients.get(r["controller"])
        if not a or not a.automatic or not a.tag or r["controller"] in out or r["client_tag"]!=a.tag:
            raise ValueError("station approval must reference exact active client approval")
        if type(r["approved_at"]) is not int or type(r["valid_until"]) is not int or not a.approved_at<=r["approved_at"]<=now or not r["approved_at"]<r["valid_until"]<=a.valid_until:
            raise ValueError("invalid station approval lifetime")
        if not isinstance(r["reviewed_by"],str) or not 1<=len(r["reviewed_by"])<=128 or any(ord(c)<32 for c in r["reviewed_by"]):
            raise ValueError("reviewer reference required")
        if not isinstance(r["checks"],dict) or set(r["checks"])!=CHECKS or any(v is not True for v in r["checks"].values()):
            raise ValueError("all station checks required")
        tag=hashlib.sha256(json.dumps({"site":cfg.site,"contract":"aos8-station-success-retry-v1","record":r},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        out[r["controller"]]=StationApproval(a.tag,r["approved_at"],r["valid_until"],tag)
    return out

def age_seconds(s):
    """Strict labeled AOS association time. Unknown layouts remain unknown."""
    if not isinstance(s,str) or len(s)>40:return None
    m=re.fullmatch(r"(?:(\d+)d:)?(?:(\d+)h:)?(?:(\d+)m:)?(\d+)s",s.strip())
    if not m:return None
    d,h,mi,se=(int(v or 0) for v in m.groups())
    if ((m[1] is not None and h>=24) or ((m[1] is not None or m[2] is not None) and mi>=60)
        or (any(v is not None for v in m.groups()[:3]) and se>=60)):
        return None
    total=d*86400+h*3600+mi*60+se
    return total if total<=365*86400 else None

@dataclass(frozen=True)
class AssociationStamp:
    ap:str
    bssid:str
    ssid:str
    aid:int
    count:int
    age:int
    at:int

def stamp(text,target,at):
    rows=associations(text)
    matches=[r for r in rows if r["active"] and r["client_mac"]==target.lower()]
    if len(matches)!=1:raise ParseError("unique active target association required")
    r=matches[0];age=age_seconds(r.get("association_time"))
    aid=r.get("aid");count=r.get("num_assoc")
    if age is None or type(aid) is not int or aid<1 or type(count) is not int or count<1:
        raise ParseError("association continuity fields missing")
    return AssociationStamp(r["ap_name"],r["bssid"],r["ssid"],aid,count,age,at)

def continuous(a,b,max_gap=180):
    """Association age/count are observed continuity evidence, not proof
    against a completely unobserved reconnect with identical metadata."""
    dt=b.at-a.at
    return (0<=dt<=max_gap and (a.ap,a.bssid,a.ssid,a.aid,a.count)==(b.ap,b.bssid,b.ssid,b.aid,b.count)
            and b.age>=a.age and abs((b.age-a.age)-dt)<=3)

class StationUnavailable(ValueError):pass
class StationBudgetExceeded(ValueError):pass

def collect(session,target,clock,wall,budget):
    """Bracket one Station Stats query with targeted association reads.
    All three sequential commands share the single detail job's deadline."""
    from .client import station_stats
    started=clock()
    def read(name):
        remaining=budget-(clock()-started)
        if remaining<.2:raise StationBudgetExceeded("detail budget exhausted")
        raw=session.run(name,target=target,timeout=remaining,idle_timeout=remaining)
        if clock()-started>budget:raise StationBudgetExceeded("detail budget exhausted")
        if re.search(r"(?im)^\s*(?:%\s*)?(?:permission denied|access denied|not authorized|authorization failed|invalid (?:input|command)|unknown command|command not found|unrecognized command|ambiguous command|incomplete command)",raw):
            raise StationUnavailable("station command or targeted association unavailable")
        return raw,int(wall())
    raw,t=read("show ap association client-mac");before=stamp(raw,target,t)
    raw,t=read("show ap debug client-stats");stats=station_stats(raw)
    if any(type(stats[k]) is not int or not 0<=stats[k]<=2**63-1 for k in ("sent","retried")):
        raise ParseError("invalid station counter range")
    raw,t=read("show ap association client-mac");after=stamp(raw,target,t)
    if not continuous(before,after,10):raise ParseError("association changed during station query")
    return {"before":before,"after":after,"counters":stats}

def successful_retry(previous,current,previous_epoch,current_epoch):
    sent=delta(previous["sent"],current["sent"],previous_epoch,current_epoch)
    retried=delta(previous["retried"],current["retried"],previous_epoch,current_epoch)
    return retry_ratio(sent,retried)
