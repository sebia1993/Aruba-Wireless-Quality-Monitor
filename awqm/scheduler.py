"""Clock-injected scheduler and bounded per-controller protection primitives."""
from dataclasses import dataclass
from collections import deque

@dataclass(frozen=True)
class Poll:
    name: str
    interval: float
    timeout: float
    priority: int
    detail: bool=False

DEFAULT_POLLS=(
    Poll("show cpuload",30,10,0),
    Poll("show lc-cluster group-membership",60,15,2),
    Poll("show ap radio-summary",60,30,1),
    Poll("show ap active details",120,60,2),
    Poll("show ap association",120,60,2),
    Poll("show user-table",180,90,5),
    Poll("show ap bss-table",300,60,5),
    Poll("show ap database long",600,60,5),
    Poll("show memory",180,10,5),
    Poll("show datapath utilization",120,30,5),
    Poll("show ap standby",300,60,5),
    Poll("show user-table standby",300,90,5),
)

class Protection:
    def __init__(self):
        self.level="unknown"
        self.high=0
        self.low=0
        self.last_cpu=None
        self.last_at=None
        self.history=deque()
    def cpu(self,value,now):
        self.last_cpu=value; self.last_at=now
        if value is None or not 0<=value<=100:
            self.level="unknown"; self.high=self.low=0
            return
        if value>=85:
            self.level="critical"; self.high=2; self.low=0
        elif value>=70:
            self.high+=1; self.low=0
            if self.high>=2 and self.level!="critical":
                self.level="limited"
        elif value<60:
            self.low+=1; self.high=0
            if self.low>=3 or self.level=="unknown":
                self.level="normal"
        else:
            self.high=self.low=0
    def effective(self,now):
        return "unknown" if self.last_at is None or now-self.last_at>90 else self.level
    def account(self,start,end,detail=False):
        if end>=start:
            self.history.append((start,end,detail))
    def budget(self,now,window,detail_only=False):
        while self.history and self.history[0][1]<=now-300:
            self.history.popleft()
        return sum(max(0,min(now,e)-max(now-window,s)) for s,e,d in self.history if not detail_only or d)
    def allowed(self,poll,now):
        if poll.name=="show cpuload":
            return True
        level=self.effective(now)
        if level=="critical":
            return poll.name=="show lc-cluster group-membership"
        if poll.detail and (level!="normal" or self.budget(now,60,True)>=10):
            return False
        return self.budget(now,300)<150

class Scheduler:
    def __init__(self,start=0,offset=0,polls=DEFAULT_POLLS,mode="safe"):
        if mode not in ("safe","standard"):
            raise ValueError("invalid mode")
        self.mode=mode
        self.polls={p.name:p for p in polls}
        self.due={p.name:start+offset for p in polls}
        self.running=None
        self.protection=Protection()
        self.skipped=0
        self.paused=False
    def interval(self,p,now):
        base=p.interval
        if self.mode=="safe":
            if p.name=="show ap radio-summary":base=max(base,180)
            if p.name=="show ap association":base=max(base,300)
        if self.protection.effective(now) in ("limited","unknown"):
            base*=2 if p.name!="show cpuload" else 1
        return base
    def next(self,now):
        if self.running or self.paused:
            return None
        ready=sorted((p for p in self.polls.values() if self.due[p.name]<=now),key=lambda p:(p.priority,self.due[p.name]))
        for p in ready:
            if not self.protection.allowed(p,now):
                self.due[p.name]=now+self.interval(p,now)
                self.skipped+=1
                continue
            self.running=(p,now)
            return p
        return None
    def finish(self,now):
        if not self.running:
            raise ValueError("no running poll")
        p,start=self.running
        self.protection.account(start,now,p.detail)
        # Do not replay missed slots.
        self.due[p.name]=max(now,start+self.interval(p,now))
        if self.due[p.name]<=now:
            self.due[p.name]=now+self.interval(p,now)
        self.running=None
    def reconnect(self,now):
        self.running=None
        self.protection=Protection()
        self.due={name:now for name in self.polls}
