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
    if previous is None or current is None or not previous.validated or not current.validated:return None
    if not previous.epoch or not previous.metric_id:return None
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
