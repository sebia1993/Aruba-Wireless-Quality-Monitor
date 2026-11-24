"""Optional, locally attested pseudonymous connection observation history.

Positive observations only. A missing row, conflicting controller, stale poll or
collection interruption is never interpreted as disconnection or seamless roam.
Raw client MAC/IP/name/SSID/BSSID never enter this ledger.
"""
from dataclasses import dataclass
from collections import defaultdict
import hashlib,hmac,json,re,secrets,time
from .service_config import safe_path
from .dpapi import protect,unprotect
from .aos8 import cleaned,norm,mac,ParseError

COMMANDS=("show user-table","show user-table standby")
CHECKS=frozenset(("active_association_mapping","bss_radio_mapping","active_table_uac_scope",
    "standby_table_uac_scope","tunnel_forwarding_scope","privacy_approval","retention_approval"))
CONTRACT="aos8-connection-history-v1"
LIMIT=30*86400

def user_table(text):
    # The published ArubaOS 8 example is one fixed-width Users table, although
    # the HTML manual splits its columns. Do not infer columns from whitespace.
    lines=cleaned(text).splitlines()
    titles=[i for i,x in enumerate(lines) if norm(x)=="users"]
    required={"ip","mac","name","role","age(d:h:m)","auth","vpn link","ap name","roaming","essid/bssid/phy","profile","forward mode","type"}
    if len(titles)!=1:raise ParseError("unique Users table required")
    found=None
    for i in range(titles[0]+1,min(titles[0]+8,len(lines)-1)):
        if not re.fullmatch(r"\s*-+(?:\s+-+)+\s*",lines[i+1]):continue
        spans=[m.start() for m in re.finditer(r"-+",lines[i+1])]
        keys=[norm(lines[i][a:b]) for a,b in zip(spans,spans[1:]+[None])]
        if len(keys)==len(set(keys)) and required<=set(keys):
            found=(i+2,spans,keys);break
    if not found:raise ParseError("unsupported user-table layout")
    pos,spans,keys=found
    rows=[];count=0;footer=False
    for line in lines[pos:]:
        if not line.strip():continue
        if re.fullmatch(r"\s*User Entries:\s*\d+(?:/\d+)?\s*",line,re.I):
            if footer:raise ParseError("duplicate footer")
            footer=True;continue
        if footer and re.fullmatch(r"\s*Curr/Cum Alloc:\s*\d+/\d+\s+Free:\s*\d+/\d+\s+Dyn:\s*\d+\s+AllocErr:\s*\d+\s+FreeErr:\s*\d+\s*",line,re.I):continue
        if footer:raise ParseError("unexpected user-table tail")
        r=dict(zip(keys,[line[a:b].strip() for a,b in zip(spans,spans[1:]+[None])]))
        ident=mac(r["mac"]);count+=1
        if count>50000:raise ParseError("user-table row limit exceeded")
        if r["roaming"].casefold()!="associated" or r["forward mode"].casefold()!="tunnel":continue
        parts=r["essid/bssid/phy"].rsplit("/",2)
        if len(parts)!=3 or not parts[0] or not parts[2] or not r["ap name"]:raise ParseError("wireless context incomplete")
        # Never keep IP/name/role/auth details from the user table.
        rows.append(dict(client_mac=ident,ap_name=r["ap name"],ssid=parts[0],bssid=mac(parts[1]),forward_mode="tunnel"))
    # Count semantics differ between releases/filters. Use positive rows only.
    return rows

@dataclass(frozen=True)
class HistoryApproval:
    radio_tag:str
    approved_at:int
    valid_until:int
    def permits(self,tag,at,now):
        return self.radio_tag==tag and self.approved_at<=at<=now<self.valid_until

def load_history_approvals(cfg,radios,retention_enabled,now=None):
    p=safe_path(cfg.root,"config/association-history.json")
    if not p.exists():return {}
    if p.stat().st_size>64000:raise ValueError("history policy too large")
    d=json.loads(p.read_text(encoding="utf-8"))
    if d=={"version":1,"enabled":False}:return {}
    if not retention_enabled:raise ValueError("30-day retention authorization required")
    if not isinstance(d,dict) or set(d)!={"version","enabled","authorized","site","contract","controllers"} or d["version"]!=1 or d["enabled"] is not True or d["authorized"] is not True or d["site"]!=cfg.site or d["contract"]!=CONTRACT:
        raise ValueError("invalid history authorization")
    now=int(time.time()) if now is None else now
    rows=d["controllers"]
    if not isinstance(rows,list) or not 1<=len(rows)<=len(radios):raise ValueError("invalid history controllers")
    out={}
    for r in rows:
        if not isinstance(r,dict) or set(r)!={"controller","radio_tag","approved_at","valid_until","reviewed_by","checks"}:raise ValueError("invalid history approval")
        a=radios.get(r["controller"])
        if not a or r["controller"] in out or r["radio_tag"]!=a.tag:raise ValueError("exact Radio approval required")
        if type(r["approved_at"]) is not int or type(r["valid_until"]) is not int or not a.approved_at<=r["approved_at"]<=now or not r["approved_at"]<r["valid_until"]<=a.valid_until:raise ValueError("invalid history lifetime")
        if not isinstance(r["reviewed_by"],str) or not 1<=len(r["reviewed_by"])<=128 or any(ord(c)<32 for c in r["reviewed_by"]):raise ValueError("reviewer reference required")
        if not isinstance(r["checks"],dict) or set(r["checks"])!=CHECKS or any(v is not True for v in r["checks"].values()):raise ValueError("all history checks required")
        out[r["controller"]]=HistoryApproval(a.tag,r["approved_at"],r["valid_until"])
    return out

def key_path(cfg):
    return safe_path(cfg.root,"secrets/association-history.dpapi")

def key_envelope(cfg,encrypt=protect):
    return encrypt(b"AWQM-history-v1\0"+cfg.site.encode("ascii")+b"\0"+secrets.token_bytes(32))

def load_key(cfg,decrypt=unprotect):
    p=key_path(cfg)
    if not p.is_file() or p.stat().st_size>65536:raise ValueError("provision association history key first")
    raw=decrypt(p.read_bytes());prefix=b"AWQM-history-v1\0"+cfg.site.encode("ascii")+b"\0"
    if not raw.startswith(prefix) or len(raw)!=len(prefix)+32:raise ValueError("history key context mismatch")
    return raw[len(prefix):]

class AssociationHistory:
    def __init__(self,store,site,key,approvals):
        if store.mode!="diagnostic" or not isinstance(key,bytes) or len(key)!=32:raise ValueError("diagnostic store and protected key required")
        self.store=store;self.site=site;self.key=key;self.approvals=dict(approvals);self.signature=None
        with store.lock,store._db(store.root/"association.db") as c:
            c.executescript("""CREATE TABLE IF NOT EXISTS association_segments(
                id TEXT NOT NULL,first_at INTEGER NOT NULL,last_at INTEGER NOT NULL,
                ap_token TEXT NOT NULL,ap_name TEXT NOT NULL,radio_token TEXT NOT NULL,band TEXT NOT NULL,
                bss_token TEXT NOT NULL,network_token TEXT NOT NULL,aac TEXT NOT NULL,
                context_token TEXT NOT NULL,auac TEXT,suac TEXT,uac_status TEXT NOT NULL,
                event TEXT NOT NULL,PRIMARY KEY(id,first_at));
                CREATE INDEX IF NOT EXISTS association_recent ON association_segments(last_at);
                CREATE INDEX IF NOT EXISTS association_by_id ON association_segments(id,last_at DESC);""")
    def token(self,kind,value):
        return hmac.new(self.key,(self.site+"\0"+kind+"\0"+value).encode("utf-8"),hashlib.sha256).hexdigest()[:32]
    def permitted(self,s,at,now):
        a=self.approvals.get(s.controller)
        return bool(a and s.field_tag and now<(s.field_until or 0) and a.permits(s.field_tag,at,now))
    def ingest(self,radios,snapshots,frames,claims,now):
        sig=tuple(sorted((s.controller,s.field_connection,s.field_tag,s.observed_at,s.association_at,
            getattr(frames.get(s.controller,(None,None,{}))[1],"observed_at",None),
            tuple((cmd,r.observed_at) for cmd,r in sorted(frames.get(s.controller,(None,None,{}))[2].items()))) for s in snapshots))
        if sig==self.signature:return 0
        by=defaultdict(list)
        for r in radios:by[(r.controller,r.ap_name,r.band,r.channel)].append(r)
        # UAC observations may be on different controllers from AAC.
        uac=defaultdict(lambda:{"active":set(),"standby":set()})
        bsses=defaultdict(list);loc=defaultdict(list)
        for s in snapshots:
            frame=frames.get(s.controller)
            if not frame or not frame[0] or frame[0].field_connection!=s.field_connection:continue
            bss=frame[1]
            if bss and 0<=now-bss.observed_at<=330 and self.permitted(s,bss.observed_at,now):
                for b in bss.data:
                    if not b["service"]:continue
                    rr=by[(s.controller,b["ap_name"],b["band"],b["channel"])]
                    anchors=[a for a in s.active if a["name"]==b["ap_name"] and a["ip"]==b["ip"] and "A" in a["roles"]]
                    if len(rr)==1 and len(anchors)==1:bsses[b["bssid"]].append((rr[0],b,s))
            for cmd,reading in frame[2].items():
                if cmd not in COMMANDS or not 0<=now-reading.observed_at<=330 or not self.permitted(s,reading.observed_at,now):continue
                for row in reading.data:
                    claim=claims.get(row["client_mac"])
                    if claim and all(claim[k]==row[k] for k in ("ap_name","bssid","ssid")):
                        uac[row["client_mac"]]["active" if cmd==COMMANDS[0] else "standby"].add((s.controller,reading.observed_at))
            if s.associations is not None and s.association_at is not None and 0<=now-s.association_at<=150 and self.permitted(s,s.association_at,now):
                for row in s.associations:
                    if row["active"]:loc[row["client_mac"]].append((row,s))
        samples=[]
        for raw,claim in claims.items():
            matches=bsses.get(claim["bssid"],())
            if len(matches)!=1:continue
            r,b,s=matches[0]
            if b["ssid"]!=claim["ssid"] or b["ap_name"]!=claim["ap_name"]:continue
            local=[(row,snap) for row,snap in loc.get(raw,()) if snap.controller==s.controller and all(row[k]==claim[k] for k in ("ap_name","bssid","ssid"))]
            if len(local)!=1:continue
            row,snap=local[0];at=snap.association_at
            if abs(r.observed_at-at)>150 or now>=snap.field_until:continue
            a={k for k,t in uac[raw]["active"] if abs(t-at)<=120};st={k for k,t in uac[raw]["standby"] if abs(t-at)<=120}
            if b["forward_mode"].casefold() not in ("tunnel","t"):au=su=None;status="not_applicable_or_unverified"
            elif len(a)>1 or len(st)>1 or a&st:au=su=None;status="conflict"
            else:
                au=next(iter(a),None);su=next(iter(st),None)
                status="positive_table_observation" if au or su else "unknown"
            samples.append((self.token("device",raw),at,self.token("ap",r.ap_id),r.ap_name,self.token("radio",r.radio_id),
                r.band,self.token("bss",claim["bssid"]),self.token("network",claim["ssid"]),r.controller,
                self.token("context",s.field_connection+"|"+r.epoch),au,su,status))
        if len(samples)>50000:raise ValueError("history sample limit exceeded")
        changed=0
        with self.store.lock,self.store._db(self.store.root/"association.db") as c:
            for ident,at,*values in samples:
                old=c.execute("""SELECT first_at,last_at,ap_token,ap_name,radio_token,band,bss_token,network_token,
                    aac,context_token,auac,suac,uac_status FROM association_segments WHERE id=? ORDER BY first_at DESC LIMIT 1""",(ident,)).fetchone()
                if old and at<=old[1]:continue
                if old and at-old[1]<=300 and tuple(values)==old[2:]:
                    c.execute("UPDATE association_segments SET last_at=? WHERE id=? AND first_at=?",(at,ident,old[0]));continue
                event="first_observation"
                if old:
                    if at-old[1]>300:event="after_observation_gap"
                    elif values[4]!=old[6] or values[5]!=old[7] or values[0]!=old[2]:event="attachment_changed"
                    elif values[6]!=old[8] or values[7]!=old[9]:event="anchor_context_changed"
                    elif tuple(values[8:])!=old[10:]:event="user_anchor_observation_changed"
                    else:event="metadata_changed"
                c.execute("INSERT INTO association_segments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(ident,at,at,*values,event))
                changed+=1
        self.signature=sig
        return changed
    def page(self,now,ident="",page=1):
        if not isinstance(ident,str) or ident and not re.fullmatch(r"[a-f0-9]{32}",ident) or type(page) is not int or not 1<=page<=10000:
            raise ValueError("exact pseudonymous ID or empty filter required")
        if not any(a.approved_at<=now<a.valid_until for a in self.approvals.values()):return {"enabled":False,"rows":[],"total":0,"page":page,"size":50}
        # 30-day query ceiling applies immediately, independent of cleanup.
        where="last_at>=? AND last_at<=?";args=[now-LIMIT,now]
        if ident:where+=" AND id=?";args.append(ident)
        if not self.store.lock.acquire(blocking=False):raise RuntimeError("storage busy")
        try:
            with self.store._db(self.store.root/"association.db") as c:
                count=c.execute("SELECT count(*) FROM association_segments WHERE "+where,args).fetchone()[0]
                rows=c.execute("SELECT id,max(first_at,?),last_at,ap_name,band,aac,auac,suac,uac_status,event FROM association_segments WHERE "+where+" ORDER BY last_at DESC,id,first_at DESC LIMIT 50 OFFSET ?",(now-LIMIT,*args,(page-1)*50)).fetchall()
        finally:self.store.lock.release()
        cols=("id","first_at","last_at","ap_name","band","aac","auac","suac","uac_status","event")
        return {"enabled":True,"rows":[dict(zip(cols,r)) for r in rows],"total":count,"page":page,"size":50,"scope":"positive_observations_only"}
