"""Bounded automatic investigation planning, not a cause classifier."""
from collections import defaultdict
import hashlib
from .ap_detail_target import binding_from_physical_id

COMMAND="show ap debug client-table"

class Selector:
    def __init__(self,per_wlc=5,per_round=2,cooldown=60,allow_attested=False):
        self.per_wlc=per_wlc;self.per_round=per_round;self.cooldown=cooldown
        self.last={};self.allow_attested=allow_attested
    def plan(self,rows,now,workers):
        """Only fresh persistent service Radio observations; group by physical AP."""
        self.last={k:v for k,v in self.last.items() if now-v<max(3600,self.cooldown*2)}
        ranked=defaultdict(dict)
        for r in rows:
            if (r.get("source")!=("attested-radio" if self.allow_attested else "synthetic") or r.get("status")!="PERSISTENT" or
                not r.get("open_incident") or not r.get("complete") or
                not 0<=now-r["observed_at"]<=90 or r["controller"] not in workers):
                continue
            score=r.get("score")
            if score is None:continue
            key=(score,-(r.get("clients") or 0),r["ap_id"])
            group=ranked[r["controller"]]
            old=group.get(r["ap_id"])
            if old is None or key<old[0]:group[r["ap_id"]]=(key,r)
        submitted=[]
        for controller,group in ranked.items():
            w=workers[controller]
            # The Worker checks live CPU/time budget and target ownership again.
            choices=sorted(group.values(),key=lambda x:x[0])[:self.per_wlc]
            pending=w.jobs.pending(now)
            active_ap={j.target for j in pending if j.name==COMMAND}
            active_ap.update(j.target for j in w.jobs.list(now) if j.name==COMMAND and j.status=="running")
            n=0
            for _,r in choices:
                if n>=self.per_round or len(active_ap)>=self.per_wlc:break
                key=(controller,r["ap_id"])
                if r["ap_name"] in active_ap or now-self.last.get(key,-1e20)<self.cooldown:continue
                rid=hashlib.sha256(f'{controller}|{r["ap_id"]}|{r["observed_at"]//60}'.encode()).hexdigest()[:32]
                try:
                    # Live selection must carry the physical identity captured
                    # by the merged Radio observation. Demo jobs stay separate.
                    kwargs={"binding":binding_from_physical_id(r["ap_id"])} if self.allow_attested else {}
                    j=w.submit_detail("auto-"+rid,COMMAND,r["ap_name"],ttl=180,**kwargs)
                except (RuntimeError,ValueError):continue
                active_ap.add(r["ap_name"]);self.last[key]=now;n+=1
                submitted.append((controller,j.job_id))
        return submitted
