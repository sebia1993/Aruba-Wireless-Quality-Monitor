"""Offline inspection of the documented 802.11ax client-info TID field.

The public CLI example exposes tid_tx_airtime but does not establish a unit,
counter/reset semantics, completeness or non-overlapping MU/OFDMA duration.
Do not sum these fields, calculate a percentage, or use them in live scoring.
Only aggregate layout/change diagnostics leave this module.
"""
import re
from dataclasses import dataclass
from .aos8 import cleaned,mac,ParseError,norm

MAX_COUNTER=(1<<64)-1
_PREFIX=r"wlan:\s*\[\d{1,10}:F:DP\]\s*"
FIELD=re.compile(_PREFIX+r"([A-Za-z0-9_]+)\s*=\s*(.*?)\s*$")
SECTION=re.compile(_PREFIX+r"(HTT_[A-Z0-9_]+_TLV):\s*$")
TID="HTT_TX_TID_STATS_V1_TLV"
PEER="HTT_PEER_DETAILS_TLV"
CLIENT=re.compile(r"client-mac\s+([0-9a-fA-F:]{17})\s+at\s+(wifi[0-9]{1,3})\s+([A-Za-z0-9_.-]{1,32})")
COMMAND=re.compile(r"cfg80211tool\s+(wifi[0-9]{1,3})\s+dp_peer_stats\s+([0-9a-fA-F:]{17})")

@dataclass(frozen=True)
class Observation:
    target:str
    interface:str
    vdev:str
    peer:int
    fields:tuple

def _integer(value):
    if not re.fullmatch(r"\d{1,20}",value):raise ParseError("invalid driver numeric field")
    result=int(value)
    if result>MAX_COUNTER:raise ParseError("driver numeric field out of range")
    return result

def parse(text):
    lines=[x.strip() for x in cleaned(text).splitlines() if x.strip()]
    if sum(norm(x)=="client info" for x in lines)!=1:
        raise ParseError("unique Client Info title required")
    headers=[(i,m) for i,x in enumerate(lines) if (m:=CLIENT.fullmatch(x))]
    commands=[(i,m) for i,x in enumerate(lines) if (m:=COMMAND.fullmatch(x))]
    if len(headers)!=1 or len(commands)!=1:
        raise ParseError("unique driver target and peer stats block required")
    hi,h=headers[0];ci,c=commands[0];target=mac(h[1])
    if hi>=ci or h[2]!=c[1] or target!=mac(c[2]):raise ParseError("driver target mismatch")
    # Stop at the next tool invocation, not arbitrary text elsewhere in
    # the large client debug output.
    tail=lines[ci+1:]
    end=next((i for i,x in enumerate(tail) if x.startswith("cfg80211tool ")),len(tail))
    tail=tail[:end]
    # Without a documented count/footer, complete coverage remains false
    # even if this subset has a consistent layout.
    peer=None;current=None;tidrows={}
    def finish():
        nonlocal current,peer
        if current is None:return
        section,fields=current
        if section==PEER:
            if peer is not None or "sw_peer_id" not in fields:raise ParseError("ambiguous peer identity")
            peer=_integer(fields["sw_peer_id"])
        elif section==TID:
            if set(fields)!={"sw_peer_id__tid_num","tid_tx_airtime"}:raise ParseError("incomplete TID sample")
            key=_integer(fields["sw_peer_id__tid_num"]);value=_integer(fields["tid_tx_airtime"])
            if key in tidrows or len(tidrows)>=256:raise ParseError("ambiguous or excessive TID samples")
            tidrows[key]=value
        current=None
    for line in tail:
        section=SECTION.fullmatch(line)
        if section:
            finish();current=(section[1],{});continue
        field=FIELD.fullmatch(line)
        if field and field[1] in ("sw_peer_id","sw_peer_id__tid_num","tid_tx_airtime"):
            expected=PEER if field[1]=="sw_peer_id" else TID
            # sw_peer_id can also occur in other legitimate TLVs (flow stats).
            if field[1]=="sw_peer_id" and current and current[0]!=PEER:continue
            if not current or current[0]!=expected or field[1] in current[1]:
                raise ParseError("ambiguous driver field context")
            current[1][field[1]]=field[2]
        elif "tid_tx_airtime" in line or "sw_peer_id__tid_num" in line:
            raise ParseError("unsupported TID field layout")
    finish()
    if peer is None or not tidrows:raise ParseError("peer or TID samples unavailable")
    return Observation(target,h[2],h[3],peer,tuple(sorted(tidrows.items())))

def inspect(previous,current=None):
    """Return only counts and validation blockers, never identifiers/values."""
    a=previous
    result={"status":"layout_observed","field_validated":False,
        "coverage_verified":False,"unit_verified":False,"counter_semantics_verified":False,
        "scope_verified":False,"airtime_percent":None,"tid_fields":len(a.fields),
        "nonzero_fields":sum(v>0 for _,v in a.fields)}
    if current is None:return result
    b=current
    same=(a.target,a.interface,a.vdev,a.peer)==(b.target,b.interface,b.vdev,b.peer)
    old=dict(a.fields);new=dict(b.fields);keys=set(old)==set(new)
    result.update(second_tid_fields=len(b.fields),second_nonzero_fields=sum(v>0 for v in new.values()),
        same_reported_context=same,same_opaque_field_keys=keys,
        increased_fields=None,decreased_fields=None,unchanged_fields=None)
    if not same or not keys:
        result["status"]="context_or_layout_changed";return result
    result.update(status="field_changes_observed",
        increased_fields=sum(new[k]>old[k] for k in old),
        decreased_fields=sum(new[k]<old[k] for k in old),
        unchanged_fields=sum(new[k]==old[k] for k in old))
    # These are observed changes, not interpreted deltas, reset detection
    # or airtime. No timestamps/association continuity have been proven.
    return result

def client_info(text):
    return inspect(parse(text))
