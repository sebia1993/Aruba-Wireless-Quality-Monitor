"""AOS 8 client diagnostics and conservative interval evidence.

Document-shaped parsers are not field-validated on the target firmware. A
successful parse never enables real Airtime automatically.
"""
from dataclasses import dataclass, asdict
import re
from .aos8 import cleaned,norm,table,mac,ip,band,field_num,channel_info,ParseError
from .model import delta, number
from .analysis import retry_ratio,client_quality

MISSING={"","-","NA","N/A"}

def count(value):
    if value.upper() in MISSING:return None
    if not re.fullmatch(r"\d+",value):raise ParseError("invalid counter")
    return int(value)

def client_table(text):
    """Client Table has no documented total footer. Require its final legend.

    Transport completeness must also be established before using samples.
    Never treat an absent MAC as disconnected using this parser.
    """
    lines=cleaned(text).splitlines()
    starts=[i for i,line in enumerate(lines) if norm(line)=="client table"]
    if len(starts)!=1:raise ParseError("Client Table not found")
    required={"mac","essid","bssid","assoc_state","tx_pkts","rx_pkts","tx_retries",
              "tx_rate","rx_rate","last_ack_snr","last_rx_snr","idle time","client health (c/r)"}
    found=None
    for i in range(starts[0]+1,min(starts[0]+8,len(lines)-1)):
        if not re.fullmatch(r"\s*-+(?:\s+-+)+\s*",lines[i+1]):continue
        spans=[m.start() for m in re.finditer(r"-+",lines[i+1])]
        cols=[norm(lines[i][a:b]) for a,b in zip(spans,spans[1:]+[None])]
        if len(cols)==len(set(cols)) and required<=set(cols):
            found=(i+2,spans,cols);break
    if not found:raise ParseError("client header missing or wrapped")
    pos,spans,cols=found
    # The documented legend closes the table; reject truncated exports.
    ends=[i for i in range(pos,len(lines)) if re.match(r"^\s*UAPSD:\s*\(",lines[i],re.I)]
    if len(ends)!=1 or not any("original capabilities" in x.lower() and "ht_state" in x.lower() for x in lines[ends[0]:]):
        raise ParseError("client table completion unverified")
    out=[]
    for line in lines[pos:ends[0]]:
        if not line.strip():continue
        vals=[line[a:b].strip() for a,b in zip(spans,spans[1:]+[None])]
        row=dict(zip(cols,vals))
        h=re.fullmatch(r"(N/A|NA|-|\d+(?:\.\d+)?)/(N/A|NA|-|\d+(?:\.\d+)?)",row["client health (c/r)"],re.I)
        if not h:raise ParseError("client/radio health pair invalid")
        d=dict(client_mac=mac(row["mac"]),bssid=mac(row["bssid"]),ssid=row["essid"],
            assoc_state=row["assoc_state"],tx_packets=count(row["tx_pkts"]),rx_packets=count(row["rx_pkts"]),
            tx_retries=count(row["tx_retries"]),tx_rate=field_num(row["tx_rate"],0,100000),
            rx_rate=field_num(row["rx_rate"],0,100000),ack_snr=field_num(row["last_ack_snr"],-20,100),
            rx_snr=field_num(row["last_rx_snr"],-20,100),idle_seconds=count(row["idle time"]),
            health=field_num(h[1],0,100),radio_health=field_num(h[2],0,100),
            row_count_verified=False)
        for cli,key in (("tx_bytes","tx_bytes"),("rx_bytes","rx_bytes")):
            d[key]=count(row[cli]) if cli in row else None
        out.append(d)
    if len({(r["client_mac"],r["bssid"]) for r in out})!=len(out):
        raise ParseError("duplicate client/BSSID")
    return out

COUNTERS={"Frames Transmitted":"sent","Success With Retry":"retried","Tx Frames Dropped":"dropped",
          "Dropped After Retry":"retry_dropped","Missed ACKs":"missed_ack","Tx Data Frames":"tx_data",
          "Rx Data Frames":"rx_data"}
def station_stats(text):
    lines=cleaned(text).splitlines()
    if sum(norm(x)=="station stats" for x in lines)!=1:raise ParseError("Station Stats missing or ambiguous")
    out={}
    for line in lines:
        for label,key in COUNTERS.items():
            m=re.fullmatch(r"\s*"+re.escape(label)+r"\s+(\d+)\s*",line,re.I)
            if m:
                if key in out:raise ParseError("counter scope ambiguous")
                out[key]=int(m[1])
    if not {"sent","retried"}<=out.keys():raise ParseError("required station counters absent")
    if out["retried"]>out["sent"]:raise ParseError("retry counter inconsistent")
    return out

def bss_table(text):
    required=("bss","ess","ip","band/ht-mode/bandwidth","ch/eirp/max-eirp","type","cur-cl","ap name","fm")
    rows=table(text,"Aruba AP BSS Table",required,r"Num APs:\s*(\d+)")
    result=[]
    for r in rows:
        info=r["band/ht-mode/bandwidth"].split("/")
        if len(info)!=3 or info[2] not in ("20MHz","40MHz","80MHz","160MHz","80+80MHz"):
            raise ParseError("unsupported BSS band/width")
        chan=r["ch/eirp/max-eirp"].split("/")
        if len(chan)!=3:raise ParseError("invalid BSS channel field")
        channel,_=channel_info(chan[0])
        role=r.get("cluster","")
        result.append(dict(bssid=mac(r["bss"]),ssid=r["ess"],ap_name=r["ap name"],ip=ip(r["ip"]),
            band=band(info[0]),width=info[2],channel=channel,forward_mode=r["fm"],role=role,
            service=r["type"].lower()=="ap",clients=count(r["cur-cl"])))
    return result

def match_bss(bssid,ssid,bsses,radios):
    """Do not assume BSSID arithmetic or choose one of several same-band radios."""
    matches={(b["ap_name"],b["band"],b["channel"],b["ssid"]) for b in bsses
             if b["bssid"]==bssid and b["service"]}
    if len(matches)!=1:return None
    name,b,ch,s=next(iter(matches))
    if s!=ssid:return None
    ids={r.radio_id for r in radios if r.ap_name==name and r.band==b and r.channel==ch}
    return next(iter(ids)) if len(ids)==1 else None

@dataclass(frozen=True)
class Sample:
    client_mac: str
    radio_id: str
    bssid: str
    ssid: str
    epoch: str
    at: int
    tx: int | None
    rx: int | None
    health: float | None
    snr: float | None
    rate: float | None
    sent: int | None=None
    retried: int | None=None
    valid: bool=True

@dataclass(frozen=True)
class Airtime:
    value: float
    method: str
    scope: str
    start: int
    end: int
    epoch: str
    validated: bool=False

def estimate_payload(groups,duration,validated=False):
    """Byte distribution by rate, not last PHY rate. No overhead included."""
    if validated is not True or number(duration,0.001,3600) is None or not groups:return None
    seconds=0
    for size,rate in groups:
        if isinstance(size,bool) or not isinstance(size,int) or size<0 or number(rate,0.001,100000) is None:return None
        seconds+=size*8/(rate*1_000_000)
    pct=100*seconds/duration
    return pct if 0<=pct<=100 else None

def interval(previous,current):
    key=lambda s:(s.client_mac,s.radio_id,s.bssid,s.ssid,s.epoch)
    if not previous or not current or not previous.valid or not current.valid or key(previous)!=key(current):return None
    duration=current.at-previous.at
    if not 1<=duration<=180:return None
    tx=delta(previous.tx,current.tx,previous.epoch,current.epoch)
    rx=delta(previous.rx,current.rx,previous.epoch,current.epoch)
    if tx is None or rx is None:return None
    sent=delta(previous.sent,current.sent,previous.epoch,current.epoch)
    retried=delta(previous.retried,current.retried,previous.epoch,current.epoch)
    retry=retry_ratio(sent,retried)
    return dict(start=previous.at,end=current.at,active=tx+rx>0,packets=tx+rx,retry=retry)

def overlap(a,b,c,d):
    return max(0,min(b,d)-max(a,c))/max(b-a,d-c) if b>a and d>c else 0

def analyze(previous,current,radio,persistent,baseline=(),airtime=None):
    """One aligned interval; confidence remains limited, never auto-confirm.

    baseline is a tuple of (earlier comparison sample, previous sample,
    current sample) for other clients on the same Radio. Missing evidence
    stays unknown rather than being interpreted as a healthy peer.
    """
    result={"quality":None,"quality_status":"unavailable","activity":"unknown",
        "airtime":None,"airtime_method":"indirect","airtime_scope":None,
        "impact_score":None,"candidate":None,"confidence":"unavailable","verified":False,
        "peer_degraded":None,"peer_comparable":0,"retry":None,"evidence":[]}
    w=interval(previous,current)
    if not w or not current.valid:return result
    result.update(activity="active" if w["active"] else "idle",retry=w["retry"])
    if not w["active"]:
        result["evidence"].append("no_packet_increase")
        return result
    quality,status=client_quality(current.health,current.snr,w["retry"])
    result.update(quality=quality,quality_status=status)
    if (not radio or not radio.valid or radio.radio_id!=current.radio_id or
        abs(radio.observed_at-current.at)>30):
        result["evidence"].append("radio_alignment_unavailable")
        return result
    if (airtime and airtime.validated is True and airtime.method in ("measured","estimated") and
        airtime.epoch==current.epoch and overlap(w["start"],w["end"],airtime.start,airtime.end)>=.8 and
        number(airtime.value,0,100) is not None and airtime.scope in ("tx","rx","whole_interval")):
        result.update(airtime=airtime.value,airtime_method=airtime.method,airtime_scope=airtime.scope)
    seen=set();degraded=0
    for old,p,c in baseline:
        if c.client_mac==current.client_mac or c.client_mac in seen:continue
        if (c.radio_id,c.ssid,c.epoch)!=(current.radio_id,current.ssid,current.epoch):continue
        pw=interval(p,c);bw=interval(old,p)
        if not pw or not bw or not pw["active"] or not bw["active"]:continue
        if abs(c.at-current.at)>30 or overlap(w["start"],w["end"],pw["start"],pw["end"])<.8:continue
        if number(p.health,0,100) is None or number(c.health,0,100) is None:continue
        seen.add(c.client_mac)
        if p.health-c.health>=20 or (pw["retry"] is not None and bw["retry"] is not None and pw["retry"]-bw["retry"]>=15):
            degraded+=1
    result["peer_comparable"]=len(seen)
    result["peer_degraded"]=degraded if seen else None
    if not persistent or radio.utilization<75:
        result["evidence"].append("persistent_congestion_not_observed")
        return result
    result["evidence"].append("persistent_radio_congestion")
    a=result["airtime"]
    whole=result["airtime_scope"]=="whole_interval"
    if a is not None and whole:
        result["evidence"].append("validated_"+result["airtime_method"]+"_occupancy")
        if seen:
            # Only one matching interval established: 50 points for temporal concurrence.
            ar=100 if a>=30 else 50 if a>=15 else 0
            v=100 if degraded>=2 else 50 if degraded==1 else 0
            result["impact_score"]=int(.6*ar+.2*50+.2*v)
        if a>=15 and degraded>=1:
            result["candidate"]="RF01"
            result["confidence"]="medium"
            result["evidence"].append("other_clients_degraded")
    # A validated low whole-interval occupancy is contrary evidence, not
    # permission to fall back to a weaker indicator and accuse the client.
    if result["candidate"] is None and a is None and degraded>=2 and quality is not None and quality<50 and w["retry"] is not None and w["retry"]>=25:
        result.update(candidate="RF03",confidence="low")
        result["evidence"].append("active_packets_high_retry_low_quality_peer_degradation")
    return result
