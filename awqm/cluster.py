"""Read-only AOS 8 cluster observations. No topology or outage inference.

The public CLI guide documents membership and cluster-wide load tables.
Membership has no count footer: a missing member is unknown, not down.
Load tables are verified against their own totals and exact member mapping.
"""
import ipaddress,re
from .aos8 import cleaned,norm,ParseError
from .controller_resources import observations as resource_observations

MEMBERSHIP="show lc-cluster group-membership"
AP_LOAD="show lc-cluster load distribution ap"
CLIENT_LOAD="show lc-cluster load distribution client"
LOAD_COMMANDS=(AP_LOAD,CLIENT_LOAD)

def _address(value):
    try:return str(ipaddress.ip_address(value))
    except ValueError as exc:raise ParseError("invalid cluster address") from exc

def _body(text,title,header):
    lines=[line.strip() for line in cleaned(text).splitlines() if line.strip()]
    starts=[i for i,line in enumerate(lines) if norm(line)==norm(title)]
    if len(starts)!=1:raise ParseError("unique cluster table required")
    i=starts[0]+1
    if i>=len(lines) or not re.fullmatch(r"-+",lines[i]):raise ParseError("cluster title separator missing")
    i+=1
    if i+1>=len(lines) or not re.fullmatch(header,norm(lines[i]),re.I):raise ParseError("unsupported cluster columns")
    if not re.fullmatch(r"-+(?:\s+-+)+",lines[i+1]):raise ParseError("cluster column separator missing")
    return lines[i+2:]

def group_membership(text):
    # Do not retain profile names or any configuration preamble (which may
    # include VRRP passphrase fields).
    rows=_body(text,"Cluster Info Table",r"type ipv[46] address priority connection-type status")
    out=[];seen=set()
    for line in rows:
        m=re.fullmatch(r"(self|peer)\s+(\S+)\s+(\d+)\s+(\S+)\s+([A-Z][A-Z -]*?)(?:\s*\(([^()]*)\))?",line,re.I)
        if not m:raise ParseError("unsupported or wrapped membership row")
        kind,addr,priority,connection,state,detail=m.groups()
        addr=_address(addr);state=state.upper().strip()
        if addr in seen or not 0<=int(priority)<=255:raise ParseError("ambiguous member row")
        if state not in ("CONNECTED","DISCONNECTED","CONNECTING","INCOMPATIBLE","NOT CONNECTED","DOWN","UNKNOWN"):
            raise ParseError("unrecognized member state")
        seen.add(addr)
        role=None
        if state=="CONNECTED" and detail:
            r=re.match(r"\s*(Leader|Member)(?:\s*,|\s*$)",detail,re.I)
            if r:role=r[1].lower()
        # Only the known link types are public; unverified states remain unknown.
        link={"l2-connected":"L2","l3-connected":"L3","n/a":None}.get(connection.lower())
        out.append({"type":kind.lower(),"address":addr,"priority":int(priority),
                    "state":state,"role":role,"link":link})
        if len(out)>32:raise ParseError("membership exceeds supported bound")
    if not out or sum(x["type"]=="self" for x in out)!=1:raise ParseError("unique self required")
    return out

def load_distribution(text,kind):
    if kind not in ("APs","Clients"):raise ValueError("invalid load type")
    rows=_body(text,"Cluster Load Distribution for "+kind,
               "type ipv[46] address active "+kind.lower()+" standby "+kind.lower())
    if not rows:raise ParseError("load footer missing")
    total=re.fullmatch(r"Total:\s*Active "+kind+r"\s+(\d+)\s+Standby "+kind+r"\s+(\d+)",rows[-1],re.I)
    if not total:raise ParseError("load footer missing")
    out=[];seen=set()
    for line in rows[:-1]:
        m=re.fullmatch(r"(self|peer)\s+(\S+)\s+(\d+)\s+(\d+)",line,re.I)
        if not m:raise ParseError("invalid load row")
        role,addr,a,s=m.groups();addr=_address(addr);a=int(a);s=int(s)
        if addr in seen or max(a,s)>2**31-1:raise ParseError("ambiguous load row")
        seen.add(addr);out.append({"type":role.lower(),"address":addr,"active":a,"standby":s})
    if len(out)>32 or sum(x["type"]=="self" for x in out)!=1:raise ParseError("unique load self required")
    if (sum(x["active"] for x in out),sum(x["standby"] for x in out))!=tuple(map(int,total.groups())):
        raise ParseError("load totals inconsistent")
    return out

def ap_load(text):return load_distribution(text,"APs")
def client_load(text):return load_distribution(text,"Clients")

class ClusterMonitor:
    """Independent controller snapshots with one representative load reader.

    No adding together duplicate cluster-wide tables. No raw addresses or
    device text are exported. Explicit conflict is not a hardware diagnosis.
    """
    def __init__(self,targets):
        self.targets={k:_address(v) for k,v in targets.items()}
        if not 1<=len(self.targets)<=4 or len(set(self.targets.values()))!=len(self.targets):
            raise ValueError("one to four unique registered cluster targets required")
        self.reverse={v:k for k,v in self.targets.items()}
        self.representative=next(iter(self.targets));self.selected_at=None

    def _fresh(self,frames,source,command,now,age):
        frame=frames.get(source) or {}
        item=frame.get("readings",{}).get(command)
        if not item or not 0<=now-item.observed_at<=age:return None
        if item.status!="success":return None
        return item

    def choose(self,frames,now,inflight=()):
        if self.selected_at is None:self.selected_at=now
        # Do not switch while an old representative can still have a load CLI
        # in flight; the next round starts only after it has returned.
        if self.representative in inflight:return self.representative
        elapsed=now-self.selected_at
        if elapsed<30:return self.representative
        candidates=[]
        for source in self.targets:
            frame=frames.get(source) or {}
            cpu=self._fresh(frames,source,"show cpuload",now,90)
            membership=self._fresh(frames,source,MEMBERSHIP,now,120)
            if frame.get("connected") and cpu and cpu.data<70 and membership:
                self_rows=[x for x in membership.data if x["type"]=="self" and x["address"]==self.targets[source] and x["state"]=="CONNECTED"]
                if len(self_rows)!=1 or any(cmd in frame.get("disabled",()) for cmd in LOAD_COMMANDS):continue
                if any(r and r.status in ("parse_error","transport_error","unsupported","permission_denied")
                    and 0<=now-r.observed_at<=120 for cmd in LOAD_COMMANDS for r in (frame.get("readings",{}).get(cmd),)):
                    continue
                candidates.append(source)
        if not candidates:return self.representative
        old=self.representative
        # Keep the representative when healthy, cross-check from another
        # controller periodically, or fail over when the old reader is unavailable.
        if old not in candidates or elapsed>=120:
            ordered=list(self.targets)
            index=ordered.index(old)
            for offset in range(1,len(ordered)+1):
                source=ordered[(index+offset)%len(ordered)]
                if source in candidates:
                    self.representative=source;self.selected_at=now;break
        return self.representative

    def report(self,frames,now):
        memberships={};cpu_rows={};unknown_members=0
        for source,address in self.targets.items():
            r=self._fresh(frames,source,MEMBERSHIP,now,120)
            cpu=self._fresh(frames,source,"show cpuload",now,90)
            cpu_rows[source]=None if cpu is None else cpu.data
            if not r:continue
            rows={x["address"]:x for x in r.data}
            if address not in rows or rows[address]["type"]!="self":continue
            # Unknown IPs are not leaked or included in registered cluster totals.
            unknown_members+=sum(x not in self.reverse for x in rows)
            memberships[source]=(r.observed_at,rows)

        nodes=[];links=[]
        for target,address in self.targets.items():
            own=memberships.get(target)
            self_state=own[1][address]["state"] if own else None
            peers=[]
            for observer,(at,rows) in memberships.items():
                if observer==target or address not in rows:continue
                r=rows[address]
                if r["type"]!="peer":continue
                peers.append(r["state"])
                links.append({"from":observer,"to":target,"state":r["state"],"link":r["link"],"observed_at":at})
            if self_state=="CONNECTED" and peers and all(v=="CONNECTED" for v in peers):status="connected_observed"
            elif self_state=="CONNECTED" and not peers:status="self_only"
            elif self_state=="CONNECTED" and any(v!="CONNECTED" for v in peers):status="peer_disagreement"
            elif any(v!="CONNECTED" for v in peers):status="peer_issue_observed"
            elif self_state and self_state!="CONNECTED":status="self_issue_observed"
            elif peers:status="peer_only"
            else:status="unknown"
            nodes.append({"controller":target,"cpu":cpu_rows[target],"state":status,
                          "role":own[1][address]["role"] if own else None,
                          "self_state":self_state,"peer_observations":len(peers),"observed_at":own[0] if own else None,
                          "resources":resource_observations(frames.get(target),now)})
        loads={}
        for kind,cmd in (("ap",AP_LOAD),("client",CLIENT_LOAD)):
            valid=[]
            for source,address in self.targets.items():
                r=self._fresh(frames,source,cmd,now,150)
                if not r or source not in memberships:continue
                member_at,member_rows=memberships[source]
                if member_rows[address]["state"]!="CONNECTED" or set(member_rows)!=set(self.reverse):continue
                rows={x["address"]:x for x in r.data}
                if set(rows)!=set(self.reverse) or rows[address]["type"]!="self":continue
                if any(x["type"]!=("self" if a==address else "peer") for a,x in rows.items()):continue
                mapped={self.reverse[a]:(x["active"],x["standby"]) for a,x in rows.items()}
                valid.append((r.observed_at,source,mapped))
            if not valid:
                loads[kind]={"status":"unknown","source":None,"observed_at":None,"rows":[],"active_total":None,"standby_total":None}
                continue
            valid.sort(key=lambda x:(x[0],x[1]==self.representative),reverse=True)
            at,source,mapped=valid[0]
            aligned=[x for x in valid[1:] if at-x[0]<=30]
            conflict=any(x[2]!=mapped for x in aligned)
            loads[kind]={"status":"conflicting_observations" if conflict else "crosschecked" if aligned else "single_source",
                         "source":source,"observed_at":at,"rows":[] if conflict else
                            [{"controller":k,"active":a,"standby":s} for k,(a,s) in sorted(mapped.items())],
                         "active_total":None if conflict else sum(x[0] for x in mapped.values()),
                         "standby_total":None if conflict else sum(x[1] for x in mapped.values())}
        return {"scope":"field_unvalidated_cluster_observations","at":now,"representative":self.representative,
                "membership_sources":len(memberships),"expected":len(self.targets),"unknown_member_observations":unknown_members,
                "nodes":nodes,"links":links,"load":loads,"service_impact":"unverified"}
