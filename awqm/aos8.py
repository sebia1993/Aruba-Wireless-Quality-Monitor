"""AOS 8 CLI parsers for documented single-line table layout.

These parsers are tested against invented, documentation-shaped fixtures, not
captured production output. A layout mismatch fails closed.
"""
from dataclasses import dataclass
import ipaddress
import re
from .model import Radio, number

class ParseError(ValueError):
    pass

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
MAC = re.compile(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}")
FAIL = re.compile(r"(?im)^\s*(?:%\s*(?:invalid|incomplete|ambiguous|error|permission)|invalid command|permission denied|access denied|command not found|unknown command|error:)")

def cleaned(text):
    if not isinstance(text,str) or len(text)>16_000_000:
        raise ParseError("invalid output size")
    text=ANSI.sub("",text).replace("\r\n","\n").replace("\r","\n")
    if FAIL.search(text) or "--More--" in text or "\x08" in text:
        raise ParseError("CLI error or incomplete paging")
    return text

def norm(s):
    return re.sub(r"\s+"," ",s.strip()).lower()

def table(text, title, required, footer):
    """Parse aligned CLI columns by separator offsets, including blank fields."""
    lines=cleaned(text).splitlines()
    titles=[i for i,line in enumerate(lines) if norm(line)==norm(title)]
    if len(titles)!=1:
        raise ParseError("table missing or ambiguous")
    start=titles[0]
    found=None
    for i in range(start+1,min(start+8,len(lines)-1)):
        if not re.fullmatch(r"\s*-+(?:\s+-+)+\s*",lines[i+1]):
            continue
        spans=[m.start() for m in re.finditer(r"-+",lines[i+1])]
        cols=[norm(lines[i][a:b]) for a,b in zip(spans,spans[1:]+[None])]
        if len(cols)==len(set(cols)) and set(required)<=set(cols):
            found=(i+2,spans,cols)
            break
    if not found:
        raise ParseError("required columns missing or wrapped header")
    pos,spans,cols=found
    rows=[]
    total=None
    ended=False
    for line in lines[pos:]:
        if not line.strip():
            continue
        count=re.fullmatch(footer,line.strip(),re.I)
        if count:
            total=int(count.group(1))
            break
        if re.match(r"^\s*(?:NF:|TD:|TD/TM/TC|Flags:|Mode:|AP followed|Cluster Role:|Channel (?:flags|followed)|PHY Details:)",line,re.I):
            ended=True
        if ended:
            continue
        if re.fullmatch(r"\s*-+(?:\s+-+)*\s*",line):
            raise ParseError("unexpected separator")
        vals=[line[a:b].strip() for a,b in zip(spans,spans[1:]+[None])]
        row=dict(zip(cols,vals))
        if not row[cols[0]]:
            raise ParseError("invalid or wrapped data row")
        rows.append(row)
    if total is None or total!=len(rows):
        raise ParseError("missing footer or row-count mismatch")
    return rows

def ip(s):
    try:
        return str(ipaddress.ip_address(s))
    except ValueError as exc:
        raise ParseError("invalid IP field") from exc

def mac(s):
    if not MAC.fullmatch(s):
        raise ParseError("invalid MAC field")
    return s.lower()

def band(s):
    s=s.strip().lower().removesuffix("ghz")
    if s not in ("2.4","5","6"):
        raise ParseError("unknown radio band")
    return s+"GHz"

def field_num(s,lo,hi):
    if s.strip().upper() in ("N/A","NA","-",""):
        return None
    try:
        v=float(s)
    except ValueError as exc:
        raise ParseError("invalid numeric field") from exc
    if number(v,lo,hi) is None:
        raise ParseError("out-of-range measurement")
    return v

def channel_info(s):
    # 80+80 blocks retain their entire raw string for later overlap analysis.
    m=re.fullmatch(r"(\d{1,3})([+\-ES]?)(?:\+\d{1,3}E)?\*?",s)
    if not m or not 1<=int(m[1])<=233:
        raise ParseError("unsupported channel")
    return int(m[1]), {"+":40,"-":40,"E":80,"S":160,"":20}[m[2]] if "E+" not in s else None

def radio_summary(text):
    required=("name","group","ap type","ip address","band","mode","eirp/maxeirp","nf/u/i","td","tm","tc")
    rows=table(text,"APs Radios information",required,r"Total Radios:\s*(\d+)")
    out=[]
    for r in rows:
        mode=r["mode"]
        active=bool(re.match(r"^AP[\^?]?:",mode))
        if not active and not re.match(r"^(?:AM|Spectrum|Disabled|Down|Off|Monitor)(?:$|[:^?])",mode,re.I):
            raise ParseError("unsupported radio mode")
        ch=None; width=None
        if active:
            ch,width=channel_info(mode.rsplit(":",1)[1])
        parts=r["nf/u/i"].split("/")
        if len(parts)!=3:
            raise ParseError("NF/U/I triplet missing")
        nf,u,i=(field_num(v,*limits) for v,limits in zip(parts,((-130,0),(0,100),(0,100))))
        out.append(dict(name=r["name"],group=r["group"],model=r["ap type"],ip=ip(r["ip address"]),
                        band=band(r["band"]),mode=mode,channel=ch,width=width,service=active,
                        noise=nf,utilization=u,interference=i,eirp_raw=r["eirp/maxeirp"],
                        td=r["td"],tm=r["tm"],tc=r["tc"]))
    return out

def inventory(text):
    rows=table(text,"AP Database",("name","group","ap type","ip address","status","switch ip","standby ip","wired mac address"),r"Total APs:\s*(\d+)")
    out=[]
    for r in rows:
        out.append(dict(name=r["name"],group=r["group"],model=r["ap type"],
                        ip=ip(r["ip address"]) if r["ip address"] not in ("N/A","-","") else None,
                        mac=mac(r["wired mac address"]),status=r["status"],
                        switch_ip=r["switch ip"],standby_ip=r["standby ip"],serial=r.get("serial #")))
    return out

def active_details(text):
    rows=table(text,"Active AP Table",("name","ip address","cluster role"),r"(?:Num|Total) APs:\s*(\d+)")
    out=[]
    for r in rows:
        roles=set(filter(None,re.split(r"[,/\s]+",r["cluster role"])))
        if not roles or not roles<={"A","SA","U","SU"}:
            # No role evidence is not AAC.
            roles=set()
        radios=[]
        for key,val in r.items():
            match=re.match(r"radio (\d+) band ",key)
            if not match or not val:
                continue
            rm=re.fullmatch(r"AP[\^?]?:(2\.4|5|6)GHz(?:-[^:]+)?:([^/]+)/([^/]+)/([^/]+)/(\d+)",val)
            if not rm:
                continue
            ch,width=channel_info(rm[2])
            radios.append(dict(radio_id=int(match[1]),band=band(rm[1]),channel=ch,width=width,clients=int(rm[5])))
        out.append(dict(name=r["name"],ip=ip(r["ip address"]),roles=sorted(roles),radios=radios))
    return out

def associations(text):
    rows=table(text,"Association Table",("name","bssid","mac","auth","assoc","essid","flags"),r"Num Clients:\s*(\d+)")
    return [dict(ap_name=r["name"],bssid=mac(r["bssid"]),client_mac=mac(r["mac"]),ssid=r["essid"],
                 active="A" in r["flags"] and r["assoc"].lower()=="y",
                 authenticated=r["auth"].lower()=="y",flags=r["flags"]) for r in rows]

def cpu_load(text):
    matches=list(re.finditer(r"(?im)^\s*user\s+([\d.]+)%,\s*system\s+([\d.]+)%,\s*idle\s+([\d.]+)%\s*$",cleaned(text)))
    if len(matches)!=1:
        raise ParseError("CPU sample missing or ambiguous")
    user,system,idle=(field_num(v,0,100) for v in matches[0].groups())
    if abs(user+system+idle-100)>1.1:
        raise ParseError("CPU percentages inconsistent")
    return round(100-idle,2)

@dataclass(frozen=True)
class ControllerSnapshot:
    controller: str
    observed_at: int
    inventory: list
    active: list
    radios: list
    associations: list | None = None

def merge_radios(snapshots, site="default", max_skew=45):
    """Only map physical radios with unique AP MAC, AAC and matching physical Radio ID.
    Return observations and unresolved evidence. Absence is never treated as Down.
    """
    if not snapshots:
        return [],[]
    newest=max(s.observed_at for s in snapshots)
    fresh=[s for s in snapshots if newest-s.observed_at<=max_skew]
    inventory_by_name={}
    for s in fresh:
        for a in s.inventory:
            inventory_by_name.setdefault((a["name"],a["ip"]),set()).add(a["mac"])
    owners={}
    for s in fresh:
        for a in s.active:
            if "A" in a["roles"]:
                owners.setdefault((a["name"],a["ip"]),[]).append((s,a))
    result=[]; unresolved=[]
    for s in fresh:
        for r in s.radios:
            if not r["service"]:
                continue
            key=(r["name"],r["ip"])
            macs=inventory_by_name.get(key,set())
            claims=owners.get(key,[])
            why=None
            if len(macs)!=1:
                why="inventory identity ambiguous"
            elif len(claims)!=1:
                why="AAC unknown or conflicting"
            elif claims[0][0].controller!=s.controller:
                # A standby/UAC observation is not a second physical radio.
                continue
            else:
                radios=[x for x in claims[0][1]["radios"] if x["band"]==r["band"] and x["channel"]==r["channel"]]
                if len(radios)!=1:
                    why="physical radio identity ambiguous"
                else:
                    a=site+":"+next(iter(macs))
                    rid=radios[0]["radio_id"]
                    # Reject multiple summary rows that would map to the same radio below.
                    result.append(Radio(a+":"+str(rid),a,r["name"],s.controller,r["band"],r["channel"],
                                        s.observed_at,r["utilization"],r["interference"],r["noise"],
                                        radios[0]["clients"],source="aos8-cli-unvalidated",
                                        epoch=f'{s.controller}:{r["band"]}:{r["mode"]}'))
            if why:
                unresolved.append(dict(controller=s.controller,name=r["name"],reason=why))
    counts={}
    for r in result:
        counts[r.radio_id]=counts.get(r.radio_id,0)+1
    for rid,n in counts.items():
        if n>1:
            unresolved.append(dict(radio_id=rid,reason="duplicate physical radio"))
    return [r for r in result if counts[r.radio_id]==1],unresolved

def merge_associations(snapshots,max_skew=45):
    if not snapshots:
        return {},{}
    newest=max(s.observed_at for s in snapshots)
    candidates={}
    for s in snapshots:
        if newest-s.observed_at>max_skew or s.associations is None:
            continue
        for a in s.associations:
            if a["active"]:
                candidates.setdefault(a["client_mac"],[]).append(a)
    active={}; conflicts={}
    for client,rows in candidates.items():
        if len({(r["ap_name"],r["bssid"],r["ssid"]) for r in rows})==1:
            active[client]=rows[0]
        else:
            conflicts[client]=rows
    return active,conflicts
