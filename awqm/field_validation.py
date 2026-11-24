"""Local operator attestation for Radio metric semantics.

A successful parse is not verification. This optional manifest is only loaded
from the ACL-protected local service configuration. It never proves actual
field checks were performed by developers or CI.
"""
import base64,hashlib,json,re,time
from dataclasses import dataclass
from .service_config import safe_path

CONTRACT="aos8-radio-v1"
CHECKS=frozenset(("nf_dbm","utilization_percent","interference_percent",
    "physical_radio_id","wired_mac_identity","active_anchor_role",
    "service_radio_mode","observation_timing"))
VERSION=re.compile(r"^8\.\d+\.\d+\.\d+$")
FINGERPRINT=re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")

def version_identity(text):
    # AOS 8 documented model/version line; ambiguous or different format
    # stays unvalidated rather than matching another version elsewhere.
    from .aos8 import cleaned,ParseError
    lines=cleaned(text)
    found=re.findall(r"(?m)^\s*ArubaOS \(MODEL:\s*([^\r\n()]{1,100})\),[ \t]*(?:\n[ \t]*)?Version[ \t]+(8\.\d+\.\d+\.\d+)[ \t]*$",lines)
    if len(found)!=1:raise ParseError("unique AOS 8 model/version required")
    return tuple(x.strip() for x in found[0])

def fingerprint(session):
    try:
        transport=session.transport.client.get_transport()
        key=transport.get_remote_server_key()
        if not transport.is_authenticated():return None
        return "SHA256:"+base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode("ascii").rstrip("=")
    except Exception:
        return None

@dataclass(frozen=True)
class Approval:
    controller:str
    ip:str
    model:str
    firmware:str
    host_key:str
    approved_at:int
    valid_until:int
    tag:str
    def permits(self,identity,key,observed_at,now,version_at):
        return (identity==(self.model,self.firmware) and key==self.host_key
                and self.approved_at<=version_at<=observed_at<=now<self.valid_until
                and now-version_at<=660)

def load_approvals(cfg,now=None):
    path=safe_path(cfg.root,"config/radio-validation.json")
    if not path.exists():return {}
    if path.stat().st_size>64000:raise ValueError("validation manifest too large")
    doc=json.loads(path.read_text(encoding="utf-8"))
    if doc=={"version":1,"enabled":False}:return {}
    if not isinstance(doc,dict) or set(doc)!={"version","enabled","site","contract","controllers"} or doc["version"]!=1 or doc["enabled"] is not True or doc["site"]!=cfg.site or doc["contract"]!=CONTRACT:
        raise ValueError("invalid validation manifest")
    targets={t.id:t for t in cfg.targets}
    now=int(time.time()) if now is None else now
    items=doc["controllers"]
    if not isinstance(items,list) or not 1<=len(items)<=len(targets):raise ValueError("invalid validation targets")
    out={}
    for d in items:
        if not isinstance(d,dict) or set(d)!={"controller","ip","model","firmware","host_key","approved_at","valid_until","reviewed_by","checks"}:
            raise ValueError("invalid attestation record")
        target=targets.get(d["controller"])
        if not target or target.ip!=d["ip"] or d["controller"] in out:raise ValueError("unregistered or duplicate attestation target")
        if not isinstance(d["model"],str) or not 1<=len(d["model"])<=100 or not isinstance(d["firmware"],str) or not VERSION.fullmatch(d["firmware"]):
            raise ValueError("invalid controller identity")
        if not isinstance(d["host_key"],str) or not FINGERPRINT.fullmatch(d["host_key"]):
            raise ValueError("invalid approved host key")
        if type(d["approved_at"]) is not int or type(d["valid_until"]) is not int or not 0<d["approved_at"]<=now or not 0<d["valid_until"]-d["approved_at"]<=365*86400:
            raise ValueError("invalid attestation lifetime")
        if not isinstance(d["reviewed_by"],str) or not 1<=len(d["reviewed_by"])<=128 or any(ord(c)<32 for c in d["reviewed_by"]):
            raise ValueError("local reviewer reference required")
        if not isinstance(d["checks"],dict) or set(d["checks"])!=CHECKS or any(v is not True for v in d["checks"].values()):
            raise ValueError("all metric and mapping checks required")
        tag=hashlib.sha256(json.dumps({"site":cfg.site,"contract":CONTRACT,"record":d},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        out[target.id]=Approval(target.id,target.ip,d["model"],d["firmware"],d["host_key"],d["approved_at"],d["valid_until"],tag)
    return out
