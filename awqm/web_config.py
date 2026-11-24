"""Explicit protected configuration for the diagnostic service HTTPS listener."""
import ipaddress,json,re,ssl
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime,timezone
from .service_config import safe_path
from .auth import validate_roles

@dataclass(frozen=True)
class WebConfig:
    bind:str
    authority:str
    port:int
    networks:tuple
    controller:str
    cert:Path
    key:Path
    roles:dict
    expires_at:int

def host_name(s):
    if not isinstance(s,str) or len(s)>253 or not s:
        raise ValueError("invalid TLS hostname")
    try:return str(ipaddress.IPv4Address(s))
    except ValueError:pass
    if not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?",x) for x in s.split(".")):
        raise ValueError("invalid TLS hostname")
    return s.lower()

def cert_expiry(cert,host,now=None):
    from cryptography import x509
    data=cert.read_bytes()
    if len(data)>2*1024*1024:raise ValueError("certificate file too large")
    leaf=x509.load_pem_x509_certificate(data)
    before=leaf.not_valid_before_utc
    after=leaf.not_valid_after_utc
    now=datetime.now(timezone.utc) if now is None else now
    if not before<=now<after:raise ValueError("TLS certificate not currently valid")
    try:san=leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:raise ValueError("certificate SAN required") from None
    try:addr=ipaddress.IPv4Address(host)
    except ValueError:
        ok=host.lower() in {x.lower() for x in san.get_values_for_type(x509.DNSName)}
    else:ok=addr in san.get_values_for_type(x509.IPAddress)
    if not ok:raise ValueError("certificate SAN mismatch")
    return int(after.timestamp())

def load_web_config(cfg):
    path=safe_path(cfg.root,"config/web.json")
    if not path.exists():return None
    if path.stat().st_size>128000:raise ValueError("web config too large")
    d=json.loads(path.read_text(encoding="utf-8"))
    if d=={"version":1,"enabled":False}:return None
    required={"version","enabled","authorized","bind","hostname","port","allowed_cidrs","auth_controller","cert","key","roles"}
    if not isinstance(d,dict) or set(d)!=required or d["version"]!=1 or d["enabled"] is not True or d["authorized"] is not True:
        raise ValueError("explicit authorized web configuration required")
    if not isinstance(d["bind"],str):raise ValueError("specific IPv4 bind required")
    bind=ipaddress.IPv4Address(d["bind"])
    if bind.is_unspecified or bind.is_multicast or bind.is_link_local or bind==ipaddress.IPv4Address("255.255.255.255"):
        raise ValueError("specific approved interface required")
    port=d["port"]
    if type(port) is not int or not 1024<=port<=65535:raise ValueError("invalid HTTPS port")
    host=host_name(d["hostname"])
    if not isinstance(d["allowed_cidrs"],list) or not 1<=len(d["allowed_cidrs"])<=32:
        raise ValueError("explicit source allowlist required")
    nets=tuple(ipaddress.IPv4Network(x,strict=True) for x in d["allowed_cidrs"])
    if any(n.prefixlen<8 or n.network_address.is_multicast for n in nets):
        raise ValueError("overly broad/invalid allowlist")
    if bind.is_loopback and any(not n.subnet_of(ipaddress.IPv4Network("127.0.0.0/8")) for n in nets):
        raise ValueError("loopback listener requires loopback allowlist")
    if d["auth_controller"] not in {t.id for t in cfg.targets}:raise ValueError("unregistered auth controller")
    roles=validate_roles(d["roles"])
    if any(u.casefold() in {t.username.casefold() for t in cfg.targets} for u in roles):
        raise ValueError("collection credentials must be separate from web identities")
    paths=[]
    for k in ("cert","key"):
        v=d[k]
        if not isinstance(v,str) or not v.replace("\\","/").startswith("secrets/"):
            raise ValueError("certificate and key must be under protected secrets")
        p=safe_path(cfg.root,v)
        if not p.is_file():raise ValueError("missing certificate/key")
        paths.append(p)
    if paths[0]==paths[1]:raise ValueError("separate certificate and key files required")
    expires=cert_expiry(paths[0],host)
    return WebConfig(str(bind),f"{host}:{port}",port,nets,d["auth_controller"],paths[0],paths[1],roles,expires)
