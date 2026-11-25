"""Offline numeric contract for a separately validated Airtime counter.

No live Aruba CLI Airtime field is enabled by this module. The caller must
establish actual counter semantics, unit, scope and continuity; PHY rate or
successful-frame counts alone cannot substitute for time occupancy.
"""
from dataclasses import dataclass
from .model import delta
from .client import Airtime

@dataclass(frozen=True)
class Counter:
    at:int
    value:int
    unit:str
    scope:str
    epoch:str
    metric_id:str
    validated:bool=False

def measured(previous,current):
    if not isinstance(previous,Counter) or not isinstance(current,Counter) or previous.validated is not True or current.validated is not True:return None
    if any(not isinstance(v,str) or not 1<=len(v)<=256 for v in (previous.epoch,previous.metric_id,current.epoch,current.metric_id)):return None
    if any(type(v) is not int or not 0<=v<=2**64-1 for v in (previous.value,current.value)):return None
    if (previous.unit,previous.scope,previous.epoch,previous.metric_id)!=(current.unit,current.scope,current.epoch,current.metric_id):return None
    if previous.unit not in ("us","ms","ns") or previous.scope not in ("tx","rx","whole_interval"):return None
    if type(previous.at) is not int or type(current.at) is not int:return None
    duration=current.at-previous.at
    if not 1<=duration<=180:return None
    change=delta(previous.value,current.value,previous.epoch,current.epoch)
    if change is None:return None
    scale={"us":1_000_000,"ms":1_000,"ns":1_000_000_000}[previous.unit]
    if change>duration*scale:return None
    return Airtime(100*(change/(duration*scale)),"measured",previous.scope,previous.at,current.at,previous.epoch,True)
