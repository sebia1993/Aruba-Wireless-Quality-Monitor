"""Protected local configuration and collection credential envelope.

No plaintext credentials in configuration. A credential is provisioned
under the designated Windows service identity and bound to its controller.
"""
from dataclasses import dataclass
from pathlib import Path
import json,ipaddress,re,os,sys,ctypes
from .dpapi import protect,unprotect

ID=re.compile(r"[A-Za-z0-9_.-]{1,64}",re.ASCII)
USER=re.compile(r"[A-Za-z0-9_.@\\-]{1,128}",re.ASCII)

@dataclass(frozen=True)
class Target:
    id:str
    ip:str
    username:str
    credential:Path

@dataclass(frozen=True)
class Config:
    root:Path
    account:str
    site:str
    known_hosts:Path
    targets:tuple

def default_root():
    if sys.platform!="win32":raise RuntimeError("Windows service required")
    return Path(os.environ.get("ProgramData",r"C:\ProgramData"))/"AWQM"

def safe_path(root,relative):
    if not isinstance(relative,str) or not relative or "\x00" in relative:
        raise ValueError("invalid relative path")
    # Reject drive/UNC/ADS and traversal even when tests run on Linux.
    parts=relative.replace("\\","/").split("/")
    if any(not p or p in (".","..") or ":" in p or p.endswith((" ",".")) for p in parts):
        raise ValueError("unsafe relative path")
    root=Path(root).absolute()
    p=root
    for part in parts:
        p=p/part
        if p.is_symlink() or getattr(p,"is_junction",lambda:False)():
            raise ValueError("reparse points not supported")
    resolved=p.resolve()
    if root.resolve() not in resolved.parents:raise ValueError("path escapes protected root")
    return resolved

def fixed_local(path):
    p=Path(path).absolute()
    if not p.is_absolute() or p.is_symlink() or getattr(p,"is_junction",lambda:False)():
        raise ValueError("local fixed path required")
    if sys.platform=="win32":
        if str(p).startswith(("\\\\","//")):raise ValueError("UNC paths prohibited")
        if ctypes.windll.kernel32.GetDriveTypeW(str(p.anchor))!=3:
            raise ValueError("fixed local drive required")
    return p.resolve()

def load_config(root):
    root=fixed_local(root)
    path=safe_path(root,"config/service.json")
    if path.stat().st_size>128000:raise ValueError("configuration too large")
    d=json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(d,dict) or set(d)!={"version","mode","authorized","windows_account","site","known_hosts","controllers"}:
        raise ValueError("invalid configuration schema")
    if d["version"]!=1 or d["mode"]!="diagnostic" or d["authorized"] is not True:
        raise ValueError("explicit diagnostic authorization required")
    account=d["windows_account"]
    if not isinstance(account,str) or not USER.fullmatch(account) or account.lower() in ("localsystem","system","localservice","networkservice"):
        raise ValueError("dedicated service account required")
    site=d["site"]
    if not isinstance(site,str) or not ID.fullmatch(site):raise ValueError("invalid site")
    if not isinstance(d["known_hosts"],str) or not d["known_hosts"].replace("\\","/").startswith("secrets/"):
        raise ValueError("known_hosts must be under secrets")
    known=safe_path(root,d["known_hosts"])
    if not known.is_file():raise ValueError("trusted known_hosts required")
    controllers=d["controllers"]
    if not isinstance(controllers,list) or not 1<=len(controllers)<=4:raise ValueError("one to four WLCs required")
    targets=[]
    for x in controllers:
        if not isinstance(x,dict) or set(x)!={"id","ip","username","credential"}:raise ValueError("invalid controller schema")
        if not isinstance(x["id"],str) or not ID.fullmatch(x["id"]) or not isinstance(x["username"],str) or not USER.fullmatch(x["username"]):
            raise ValueError("invalid controller ID or username")
        if not isinstance(x["ip"],str):raise ValueError("management IP literal required")
        addr=ipaddress.ip_address(x["ip"])
        if addr.is_multicast or addr.is_unspecified or addr.is_loopback:raise ValueError("invalid management IP")
        relative=x["credential"]
        if not isinstance(relative,str) or not relative.replace("\\","/").startswith("secrets/") or not relative.endswith(".dpapi"):
            raise ValueError("credential must be a .dpapi file under secrets")
        targets.append(Target(x["id"],str(addr),x["username"],safe_path(root,relative)))
    if any(len({getattr(t,field) for t in targets})!=len(targets) for field in ("id","ip","credential")):
        raise ValueError("duplicate controller ID, address or credential file")
    return Config(root,account,site,known,tuple(targets))

def envelope(target,password,encrypt=protect):
    if not isinstance(password,str) or not 1<=len(password)<=1024:raise ValueError("invalid password")
    data={"v":1,"id":target.id,"ip":target.ip,"username":target.username,"password":password}
    return encrypt(json.dumps(data,ensure_ascii=False,separators=(",",":")).encode("utf-8"))

def secret(target,decrypt=unprotect):
    if not target.credential.is_file() or target.credential.stat().st_size>65536:
        raise ValueError("missing or invalid DPAPI credential")
    data=json.loads(decrypt(target.credential.read_bytes()).decode("utf-8"))
    if set(data)!={"v","id","ip","username","password"} or data["v"]!=1 or (data["id"],data["ip"],data["username"])!=(target.id,target.ip,target.username):
        raise ValueError("credential target binding mismatch")
    if not isinstance(data["password"],str) or not 1<=len(data["password"])<=1024:raise ValueError("invalid credential")
    return data["password"]

def load_profile(cfg):
    """Safe startup unless the protected local profile is explicitly enabled."""
    p=safe_path(cfg.root,"config/collection.json")
    if not p.exists():return "safe"
    if p.stat().st_size>4096:raise ValueError("collection profile too large")
    d=json.loads(p.read_text(encoding="utf-8"))
    if d=={"version":1,"profile":"safe"}:return "safe"
    if d=={"version":1,"profile":"standard","authorized":True}:return "standard"
    raise ValueError("unsupported or unauthorized collection profile")
