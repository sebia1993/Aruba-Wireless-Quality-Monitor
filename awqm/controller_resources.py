"""Conservative, separately authorized WLC resource observations.

Document-shaped parsers only. Never infer WLC failure, radio root cause,
memory pressure or total datapath capacity from these measurements.
"""
import json,re,time
from dataclasses import dataclass
from .aos8 import cleaned,norm,ParseError
from .service_config import safe_path

MEMORY="show memory"
DATAPATH="show datapath utilization"
COMMANDS=(MEMORY,DATAPATH)
AGES={MEMORY:360,DATAPATH:240}
CHECKS=frozenset(("memory_layout_and_unit","datapath_layout_and_cpu_inventory",
    "datapath_1s_4s_percent","read_only_support_permission"))
CONTRACT="aos8-wlc-resources-v1"
CPU_TYPES=frozenset(("SP","SPGW","FP","FPGW","DPI","CRYP","SPSPARE"))

def memory(text):
    # The public AOS8 CLI reference documents the summary in Kb.
    # Do not interpret per-process tables or a Linux free/available column.
    m=re.fullmatch(r"\s*Memory\s*\(Kb\)\s*:\s*total\s*:\s*(\d{1,15})\s*,\s*used\s*:\s*(\d{1,15})\s*,\s*free\s*:\s*(\d{1,15})\s*",cleaned(text),re.I)
    if not m:raise ParseError("unsupported memory summary")
    total,used,free=map(int,m.groups())
    if total<=0 or used+free!=total:raise ParseError("inconsistent memory counts")
    return {"total_kb":total,"used_kb":used,"free_kb":free,"reported_used_percent":round(100*used/total,2)}

def datapath(text):
    # Support the untyped AOS8 documented table, plus the explicitly
    # labelled typed variant as a candidate for local layout validation.
    lines=[x.strip() for x in cleaned(text).splitlines() if x.strip()]
    if not lines or norm(lines[0])!="datapath network processor utilization":
        raise ParseError("datapath title missing")
    sep=lambda s:bool(re.fullmatch(r"[+\-]{5,}",s) and "-" in s)
    if len(lines)<5 or not sep(lines[1]):raise ParseError("datapath table missing")
    if norm(lines[2]).replace("|"," ").strip().split() not in (
        ["cpu","utilization","during","past"],["cpu","cpu","utilization","during","past"]):
        raise ParseError("unsupported datapath group header")
    header=norm(lines[3].replace("|"," "))
    typed=header=="type id 1 sec 4 secs 64 secs"
    if not typed and header!="cpu 1 sec 4 secs 64 secs":raise ParseError("unsupported datapath columns")
    if not sep(lines[4]):raise ParseError("datapath separator missing")
    out=[];seen=set();allocation=None;tail=lines[5:]
    for index,line in enumerate(tail):
        if norm(line)=="datapath cpu allocation summary":
            if not typed:raise ParseError("unexpected allocation summary")
            allocation=tail[index+1:];break
        if sep(line):
            if index==len(tail)-1:break
            if typed and norm(tail[index+1])=="datapath cpu allocation summary":
                allocation=tail[index+2:];break
            raise ParseError("unexpected datapath separator")
        pattern=(r"([A-Z]{2,8})\s*\|\s*" if typed else "")+r"(\d{1,4})\s*\|\s*(\d{1,3}(?:\.\d{1,3})?)%\s*\|\s*(\d{1,3}(?:\.\d{1,3})?)%\s*\|\s*(\d{1,3}(?:\.\d{1,3})?)%\s*\|"
        m=re.fullmatch(pattern,line.strip().lstrip("|").strip())
        if not m:raise ParseError("unsupported or partial datapath row")
        vals=m.groups();kind=vals[0] if typed else None
        cpu=int(vals[-4]);u1,u4,u64=map(float,vals[-3:])
        if cpu>4095 or cpu in seen or kind is not None and kind not in CPU_TYPES or any(not 0<=x<=100 for x in (u1,u4,u64)):
            raise ParseError("ambiguous or invalid datapath CPU")
        seen.add(cpu);out.append({"id":cpu,"type":kind,"percent_1s":u1,"percent_4s":u4,
            "reported_64s_percent":u64})
        if len(out)>256:raise ParseError("datapath CPU limit exceeded")
    if not out:raise ParseError("no datapath CPU observations")
    # A typed summary, when provided, must agree with the observed rows.
    if allocation is not None:
        counts={}
        known={"Slow Path (SP)":"SP","Slow Path Gateway (SPGW)":"SPGW",
          "Fast Path (FP)":"FP","Fast Path Gateway (FPGW)":"FPGW",
          "DPI":"DPI","Crypto (CRYP)":"CRYP","Slow Path Spare (SPSPARE)":"SPSPARE"}
        for line in allocation:
            for item in line.split(","):
                m=re.fullmatch(r"\s*(.+?)\s*:\s*(\d{1,4})\s*",item)
                if not m or m[1] not in known or known[m[1]] in counts:raise ParseError("invalid CPU allocation summary")
                counts[known[m[1]]]=int(m[2])
        if not counts or any(counts.get(k,0)!=sum(r["type"]==k for r in out) for k in CPU_TYPES):
            raise ParseError("datapath allocation counts inconsistent")
    return {"rows":sorted(out,key=lambda r:r["id"]),"layout":"typed" if typed else "untyped",
            "allocation_checked":allocation is not None,"row_count_verified":False}

@dataclass(frozen=True)
class ResourceApproval:
    radio_tag:str
    approved_at:int
    valid_until:int
    cpu_ids:tuple
    def permits(self,tag,at,now):
        return self.radio_tag==tag and self.approved_at<=at<=now<self.valid_until

def load_resource_approvals(cfg,radio_approvals,now=None):
    p=safe_path(cfg.root,"config/resource-validation.json")
    if not p.exists():return {}
    if p.stat().st_size>64000:raise ValueError("resource manifest too large")
    d=json.loads(p.read_text(encoding="utf-8"))
    if d=={"version":1,"enabled":False}:return {}
    if not isinstance(d,dict) or set(d)!={"version","enabled","site","contract","controllers"} or type(d["version"]) is not int or d["version"]!=1 or d["enabled"] is not True or d["site"]!=cfg.site or d["contract"]!=CONTRACT:
        raise ValueError("invalid resource validation manifest")
    now=int(time.time()) if now is None else now
    rows=d["controllers"]
    if not isinstance(rows,list) or not 1<=len(rows)<=len(radio_approvals):raise ValueError("invalid resource controllers")
    out={}
    for r in rows:
        if not isinstance(r,dict) or set(r)!={"controller","radio_tag","approved_at","valid_until","reviewed_by","cpu_ids","checks"}:
            raise ValueError("invalid resource record")
        a=radio_approvals.get(r["controller"])
        if not a or r["controller"] in out or r["radio_tag"]!=a.tag:raise ValueError("exact Radio approval required")
        if type(r["approved_at"]) is not int or type(r["valid_until"]) is not int or not a.approved_at<=r["approved_at"]<=now or not r["approved_at"]<r["valid_until"]<=a.valid_until:
            raise ValueError("invalid resource lifetime")
        if not isinstance(r["reviewed_by"],str) or not 1<=len(r["reviewed_by"])<=128 or any(ord(c)<32 for c in r["reviewed_by"]):
            raise ValueError("local reviewer reference required")
        ids=r["cpu_ids"]
        if not isinstance(ids,list) or not 1<=len(ids)<=256 or any(type(i) is not int or not 0<=i<=4095 for i in ids) or len(set(ids))!=len(ids):
            raise ValueError("explicit unique CPU ID inventory required")
        if not isinstance(r["checks"],dict) or set(r["checks"])!=CHECKS or any(v is not True for v in r["checks"].values()):
            raise ValueError("all resource checks required")
        out[r["controller"]]=ResourceApproval(a.tag,r["approved_at"],r["valid_until"],tuple(sorted(ids)))
    return out

def observations(frame,now):
    """Only already attested, matching-generation observations, with TTL."""
    if not frame or not frame.get("connected"):
        return {k:{"status":"unavailable","observed_at":None,"data":None} for k in ("memory","datapath")}
    gate=frame.get("resource_validation")
    result={}
    for label,cmd in (("memory",MEMORY),("datapath",DATAPATH)):
        value={"status":"disabled","observed_at":None,"data":None}
        if gate is not None:
            r=frame.get("readings",{}).get(cmd)
            value["status"]="unavailable"
            if r is not None:
                value["observed_at"]=r.observed_at
                if r.status!="success":value["status"]=r.status
                elif not 0<=now-r.observed_at<=AGES[cmd]:value["status"]="stale"
                elif not gate.get("valid") or not gate["approved_at"]<=r.observed_at<=now<gate["until"]:
                    value["status"]="attestation_unavailable"
                elif label=="datapath" and tuple(x["id"] for x in r.data["rows"])!=tuple(gate["cpu_ids"]):
                    value["status"]="cpu_inventory_mismatch"
                else:
                    value["status"]="observed";value["valid_until"]=gate["until"]
                    if label=="memory":
                        value["data"]={k:r.data[k] for k in ("total_kb","used_kb","free_kb","reported_used_percent")}
                    else:
                        rows=[{k:x[k] for k in ("id","type","percent_1s","percent_4s")} for x in r.data["rows"]]
                        value["data"]={"rows":rows,"cpu_count":len(rows),
                            "max_1s_percent":max(x["percent_1s"] for x in rows),
                            "max_4s_percent":max(x["percent_4s"] for x in rows),
                            "64s_omitted":True}
        result[label]=value
    return result
