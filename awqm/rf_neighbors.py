"""AOS 8.9+ RF neighbor parser and conservative channel-overlap evidence.

Offline parser results are document-shaped, not field validated. In particular
ARM neighbor output has no documented row-count footer, an AP-level query
does not identify a receiving physical Radio, and Last Update needs a
verified timezone and clock. Do not make a negative inference from absence.
"""
from dataclasses import dataclass
from datetime import datetime
import re
from .aos8 import cleaned,norm,mac,band,channel_info,field_num,ParseError

def arm_neighbors(text):
    lines=cleaned(text).splitlines()
    starts=[i for i,x in enumerate(lines) if norm(x)=="arm neighbors"]
    if len(starts)!=1:raise ParseError("unique ARM Neighbors table required")
    required={"bssid","essid","phy-type","channel","snr","tx-power","pl (db)","ap flags","last update (total updates)"}
    found=None
    for i in range(starts[0]+1,min(starts[0]+8,len(lines)-1)):
        if not re.fullmatch(r"\s*-+(?:\s+-+)+\s*",lines[i+1]):continue
        spans=[m.start() for m in re.finditer(r"-+",lines[i+1])]
        cols=[norm(lines[i][a:b]) for a,b in zip(spans,spans[1:]+[None])]
        if len(cols)==len(set(cols)) and required<=set(cols):
            found=(i+2,spans,cols);break
    if not found:raise ParseError("unsupported or wrapped ARM table")
    pos,spans,cols=found
    out=[];seen=set()
    for line in lines[pos:]:
        if not line.strip():continue
        values=[line[a:b].strip() for a,b in zip(spans,spans[1:]+[None])]
        row=dict(zip(cols,values))
        b=mac(row["bssid"]);phy=band(row["phy-type"])
        ch,width=channel_info(row["channel"])
        flags=row["ap flags"].lower()
        if flags not in ("active","passive","indirect"):raise ParseError("unknown discovery method")
        snr=field_num(row["snr"],-20,100)
        tx=field_num(row["tx-power"],-30,60)
        loss=field_num(row["pl (db)"],0,250)
        last=row["last update (total updates)"]
        stamp=None;updates=None
        if last and last.upper() not in ("NA","N/A","-"):
            m=re.fullmatch(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*\((\d+)\)",last)
            if not m:raise ParseError("unsupported neighbor update timestamp")
            try:datetime.strptime(m[1],"%Y-%m-%d %H:%M:%S")
            except ValueError as exc:raise ParseError("invalid neighbor update timestamp") from exc
            stamp=m[1];updates=int(m[2])
        key=(b,phy)
        if key in seen:raise ParseError("ambiguous duplicate neighbor")
        seen.add(key)
        # ESSID may include a source BSSID. Keep neither in the parsed neighbor
        # record: the discovered BSSID and verified mapping are sufficient.
        out.append(dict(bssid=b,band=phy,primary_channel=ch,width_candidate=width,
            channel_raw=row["channel"],snr=snr,tx_power=tx,path_loss=loss,discovery=flags,
            last_update_raw=stamp,total_updates=updates,row_count_verified=False,
            timestamp_verified=False,receiving_radio_verified=False))
        if len(out)>20000:raise ParseError("neighbor table exceeds limit")
    return out

def center_mhz(phy,ch):
    if type(ch) is not int:return None
    if phy=="2.4GHz":
        return 2484 if ch==14 else 2407+5*ch if 1<=ch<=13 else None
    if phy=="5GHz":
        # Only commonly deployed 20 MHz Wi-Fi channel grid, not a country
        # channel-availability determination.
        if ch in tuple(range(36,65,4))+tuple(range(100,145,4))+tuple(range(149,178,4)):
            return 5000+5*ch
    if phy=="6GHz":
        if ch==2:return 5935
        if 1<=ch<=233 and (ch-1)%4==0:return 5950+5*ch
    return None

@dataclass(frozen=True)
class ChannelBlock:
    band:str
    primary:int
    subchannels:tuple
    verified:bool=False
    def span(self):
        """Explicit constituent channels only. Do not guess E/S/80+80 blocks."""
        if not self.verified or not isinstance(self.subchannels,tuple) or len(self.subchannels) not in (1,2,4,8):
            return None
        if self.primary not in self.subchannels or len(set(self.subchannels))!=len(self.subchannels):return None
        if self.band=="2.4GHz" and len(self.subchannels)>2:return None
        centers=[center_mhz(self.band,c) for c in self.subchannels]
        if any(c is None for c in centers):return None
        centers=sorted(centers)
        if any(b-a!=20 for a,b in zip(centers,centers[1:])):return None
        # Nominal channel width for overlap analysis, not a spectral mask.
        return centers[0]-10,centers[-1]+10

def overlap(first,second):
    if first.band!=second.band:
        return {"relation":"different_band","overlap_mhz":0}
    a,b=first.span(),second.span()
    if a is None or b is None:return {"relation":"unknown","overlap_mhz":None}
    amount=max(0,min(a[1],b[1])-max(a[0],b[0]))
    relation="same_block" if a==b else "overlapping_block" if amount>0 else "disjoint"
    return {"relation":relation,"overlap_mhz":amount}

def candidate(first,second,first_block,second_block,neighbor,now,neighbor_at=None,
              mapping_verified=False,field_validated=False,first_persistent=False,second_persistent=False):
    """Produce a bounded RF05/RF06 *candidate* from aligned validated evidence.

    The caller must verify a direct receiving-Radio/discovered-BSSID mapping
    and device-provided neighbor timestamp before supplying those inputs.
    Passive/indirect discovery, missing timestamps, or single-sided congestion
    do not establish current co-channel contention. No actual service impact.
    """
    geometry=overlap(first_block,second_block)
    result={"relation":geometry["relation"],"overlap_mhz":geometry["overlap_mhz"],
            "candidate":None,"confidence":"unavailable","verified":False,"service_impact":"unverified","reason":"insufficient_evidence"}
    if geometry["relation"] not in ("same_block","overlapping_block"):
        result["reason"]="channel_overlap_not_established";return result
    if not (mapping_verified and field_validated and first.ap_id!=second.ap_id and
            first.radio_id!=second.radio_id and first.band==first_block.band and second.band==second_block.band and
            first.channel==first_block.primary and second.channel==second_block.primary and
            neighbor.get("band")==second.band and neighbor.get("primary_channel")==second.channel and
            neighbor.get("width_candidate")==20*len(second_block.subchannels)):
        result["reason"]="mapping_or_semantics_unverified";return result
    if neighbor.get("discovery")!="active" or type(neighbor_at) is not int or not 0<=now-neighbor_at<=90:
        result["reason"]="direct_neighbor_update_unverified";return result
    if not (first.valid and second.valid and 0<=now-first.observed_at<=90 and 0<=now-second.observed_at<=90
            and abs(first.observed_at-second.observed_at)<=30 and abs(neighbor_at-first.observed_at)<=30):
        result["reason"]="unaligned_or_stale_observations";return result
    if not first_persistent or not second_persistent or first.utilization<75 or second.utilization<75:
        result["reason"]="concurrent_persistent_congestion_not_observed";return result
    result.update(candidate="RF05" if geometry["relation"]=="same_block" else "RF06",
                  confidence="medium",reason="verified_direct_neighbor_and_aligned_congestion")
    return result
