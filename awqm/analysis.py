from dataclasses import dataclass, replace
from .model import Radio, number, delta

def risk(v, warn, danger):
    return 100 if v >= danger else 50 if v >= warn else 0

def radio_score(row: Radio):
    if not row.valid:
        return None
    nf = 100 if row.noise > -80 else 50 if row.noise > -90 else 0
    return int(100 - .5*risk(row.utilization,50,75) - .3*risk(row.interference,10,30) - .2*nf)

def retry_ratio(sent, retried):
    if sent is None or retried is None or sent < 100 or not 0 <= retried <= sent:
        return None
    return 100*retried/sent

def client_quality(health, snr, retry):
    if number(health,0,100) is None or number(snr,-20,100) is None:
        return None, "unavailable"
    h = 100 if health<50 else 50 if health<80 else 0
    s = 100 if snr<15 else 50 if snr<25 else 0
    if number(retry,0,100) is None:
        return round(100-(.4*h+.35*s)/.75), "partial"
    return round(100-.4*h-.35*s-.25*risk(retry,10,25)), "complete"

def client_impact(airtime, method, scope, active, concurrent, peers, valid):
    """No estimated score unless scope and inputs are comparable; never infer causation."""
    if (not valid or active is not True or method not in ("measured","estimated")
        or scope != "whole_interval" or number(airtime,0,100) is None
        or concurrent not in (0,1,3) or not isinstance(peers,int) or peers<0):
        return None
    a = risk(airtime,15,30)
    c = {0:0,1:50,3:100}[concurrent]
    v = 0 if peers==0 else 50 if peers==1 else 100
    return {"score":int(.6*a+.2*c+.2*v),"method":method,
            "candidate":airtime>=15 and concurrent>0 and peers>0,
            "confidence":"medium" if method=="estimated" else ("high" if concurrent==3 and peers>=2 else "medium"),
            "verified":False}

@dataclass(frozen=True)
class State:
    status: str = "NORMAL"
    bad: int = 0
    good: int = 0
    open_incident: bool = False
    last_at: int | None = None
    epoch: str | None = None

def advance(state, row: Radio, max_gap=90):
    # A repeat or out-of-order sample cannot count towards persistence.
    if state.last_at is not None and row.observed_at <= state.last_at:
        return state
    continuity = (state.last_at is not None and 0 < row.observed_at-state.last_at <= max_gap
                  and state.epoch == row.epoch)
    base = replace(state, bad=state.bad if continuity else 0, good=state.good if continuity else 0,
                   last_at=row.observed_at,epoch=row.epoch)
    if not row.valid:
        return replace(base,status="UNKNOWN",bad=0,good=0)
    bad = row.utilization>=75 or row.interference>=30 or row.noise>-80
    clear = row.utilization<65 and row.interference<20 and row.noise<=-85
    if bad:
        count=base.bad+1
        opened=base.open_incident or count>=3
        return replace(base,status="PERSISTENT" if opened else "DETECTED",bad=count,good=0,open_incident=opened)
    if base.open_incident:
        if clear:
            count=base.good+1
            return replace(base,status="RESOLVED" if count>=3 else "RECOVERING",bad=0,good=count,open_incident=count<3)
        return replace(base,status="PERSISTENT",bad=0,good=0)
    watch = row.utilization>=50 or row.interference>=10 or row.noise>-90
    return replace(base,status="WATCH" if watch else "NORMAL",bad=0,good=0)
