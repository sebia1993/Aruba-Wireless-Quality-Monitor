"""Locally attested RF observation incidents, not service-outage declarations.

The same core write transaction stores the quality transition and evidence.
No live client cause is inferred. Reviews are append-only, bounded, idempotent
and tied to the exact evidence revision reviewed.
"""
import json,re,uuid,time
from dataclasses import asdict
from types import SimpleNamespace
from contextlib import contextmanager

POLICY="balanced-radio-v1"
REVIEW_VERDICTS=frozenset(("supported","not_supported","inconclusive"))
REASONS=frozenset(("radio_evidence","other_evidence","insufficient_evidence","conflicting_evidence"))
IDENT=re.compile(r"^[0-9a-f]{32}$")
REQUEST=re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")
USER=re.compile(r"^[A-Za-z0-9_.@\\-]{1,128}$")

def evidence_for(r,state,score):
    candidates=[]
    if r.valid and r.utilization>=75:
        candidates.append({"code":"RF04","label":"Radio congestion observed",
            "confidence":"medium" if state.status=="PERSISTENT" else "low",
            "basis":["utilization_at_or_above_75"],"limit":"Specific contributing client and service impact unknown"})
    if r.valid and (r.interference>=30 or r.noise>-80):
        candidates.append({"code":"UNKNOWN","label":"RF interference/noise source unconfirmed",
            "confidence":"low","basis":[k for k,b in (("interference_at_or_above_30",r.interference>=30),
                         ("noise_above_minus_80",r.noise>-80)) if b],
            "limit":"External, co-channel, overlapping-channel and other sources not distinguished"})
    if not candidates:
        candidates.append({"code":"UNKNOWN","label":"Insufficient cause evidence",
            "confidence":"unavailable","basis":[],"limit":"No client/neighbor cause established"})
    return {"observed_at":r.observed_at,"utilization":r.utilization,"interference":r.interference,
            "noise":r.noise,"channel":r.channel,"band":r.band,"complete":r.complete,
            "quality_score":score,"policy":POLICY,"risk_status":state.status,
            "candidates":candidates,"service_impact":"unverified"}

class Incidents:
    def __init__(self,store,clock=time.time):
        if store.mode!="diagnostic":raise ValueError("diagnostic store required")
        self.store=store;self.clock=clock
        with store.lock,store._db(store.root/"core.db") as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS rf_incidents(
              incident_id TEXT PRIMARY KEY, radio_id TEXT NOT NULL, context TEXT NOT NULL,
              ap_name TEXT NOT NULL, controller TEXT NOT NULL, band TEXT NOT NULL,
              first_at INTEGER NOT NULL, last_at INTEGER NOT NULL,
              persistent_at INTEGER, ended_at INTEGER, state TEXT NOT NULL,
              evidence_rev INTEGER NOT NULL, evidence TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS rf_incident_open_context
              ON rf_incidents(radio_id,context) WHERE ended_at IS NULL;
            CREATE INDEX IF NOT EXISTS rf_incident_recent ON rf_incidents(last_at DESC,incident_id);
            CREATE TABLE IF NOT EXISTS rf_incident_events(
              incident_id TEXT NOT NULL, revision INTEGER NOT NULL, at INTEGER NOT NULL,
              state TEXT NOT NULL, evidence TEXT NOT NULL,
              PRIMARY KEY(incident_id,revision));
            CREATE TABLE IF NOT EXISTS rf_incident_reviews(
              request_id TEXT PRIMARY KEY, incident_id TEXT NOT NULL, evidence_rev INTEGER NOT NULL,
              username TEXT NOT NULL, at INTEGER NOT NULL, verdict TEXT NOT NULL,
              reason TEXT NOT NULL, candidate TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS rf_review_incident ON rf_incident_reviews(incident_id,at DESC);
            """)
    @contextmanager
    def transaction(self):
        # Same serialized writer lock as telemetry. Never wait on a long
        # aggregation/backup from a web request.
        if not self.store.lock.acquire(blocking=False):raise RuntimeError("storage busy")
        try:
            with self.store._db(self.store.root/"core.db") as c:yield c
        finally:self.store.lock.release()
    def observe(self,c,r,context,state,score):
        """Called inside the quality-ledger transaction, only for new observations."""
        t=r.observed_at
        evidence=evidence_for(r,state,score)
        payload=json.dumps(evidence,sort_keys=True,allow_nan=False)
        # An old open context must not be called recovered after a failover,
        # channel change, service restart or new attestation.
        previous=c.execute("""SELECT incident_id,evidence_rev,evidence FROM rf_incidents
            WHERE radio_id=? AND context<>? AND ended_at IS NULL""",(r.radio_id,context)).fetchall()
        for ident,rev,old in previous:
            ev=json.loads(old);ev["reason"]="observation_context_changed"
            data=json.dumps(ev,sort_keys=True,allow_nan=False)
            c.execute("""UPDATE rf_incidents SET state='CONTEXT_LOST',ended_at=?,evidence_rev=?,evidence=?
                WHERE incident_id=?""",(t,rev+1,data,ident))
            c.execute("INSERT INTO rf_incident_events VALUES(?,?,?,?,?)",(ident,rev+1,t,"CONTEXT_LOST",data))
        old=c.execute("""SELECT incident_id,last_at,persistent_at,state,evidence_rev,evidence
            FROM rf_incidents WHERE radio_id=? AND context=? AND ended_at IS NULL""",
            (r.radio_id,context)).fetchone()
        if old is None:
            if state.status not in ("DETECTED","PERSISTENT"):return None
            ident=uuid.uuid4().hex
            persistent=t if state.status=="PERSISTENT" else None
            c.execute("INSERT INTO rf_incidents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ident,r.radio_id,context,r.ap_name,r.controller,r.band,t,t,persistent,None,state.status,1,payload))
            c.execute("INSERT INTO rf_incident_events VALUES(?,?,?,?,?)",(ident,1,t,state.status,payload))
            return ident
        ident,last,persistent,old_state,rev,old_evidence=old
        if t<=last:return ident
        status=state.status
        if status=="UNKNOWN":
            status="UNCONFIRMED"
        elif status in ("WATCH","NORMAL"):
            if persistent is not None:
                status="UNCONFIRMED" # defensive; never close a persistent event on one sample
            else:status="TRANSIENT"
        if status=="PERSISTENT" and persistent is None:persistent=t
        ended=t if status in ("RESOLVED","TRANSIENT") else None
        # Preserve evidence at every changed state/candidate or new observed
        # worst metric; otherwise keep the prior reviewable evidence revision.
        old_ev=json.loads(old_evidence)
        codes=lambda v:tuple((x["code"],x["confidence"],tuple(x["basis"])) for x in v["candidates"])
        changed=(status!=old_state or codes(evidence)!=codes(old_ev)
            or (score is not None and (old_ev.get("quality_score") is None or score<old_ev["quality_score"])))
        if changed:
            rev+=1
            c.execute("INSERT INTO rf_incident_events VALUES(?,?,?,?,?)",(ident,rev,t,status,payload))
        else:payload=old_evidence
        c.execute("""UPDATE rf_incidents SET last_at=?,persistent_at=?,ended_at=?,state=?,evidence_rev=?,evidence=?
            WHERE incident_id=?""",(t,persistent,ended,status,rev,payload,ident))
        return ident
    def record_neighbors(self,r,quality,at,matches):
        """Append a separately timed, bounded candidate evidence revision."""
        ident=quality.get("incident_id")
        if not ident or not IDENT.fullmatch(ident) or quality.get("status")!="PERSISTENT" or quality.get("observed_at")!=r.observed_at:
            return False
        if not r.valid or not 0<=at-r.observed_at<=90 or not 1<=len(matches)<=20:
            return False
        if any(x.get("candidate") not in ("RF05","RF06") or x.get("confidence")!="medium" for x in matches):
            return False
        with self.store.lock,self.store._db(self.store.root/"core.db") as c:
            row=c.execute("SELECT state,last_at,evidence_rev,evidence,ended_at FROM rf_incidents WHERE incident_id=? AND radio_id=?",(ident,r.radio_id)).fetchone()
            if not row or row[0]!="PERSISTENT" or row[1]!=r.observed_at or row[4] is not None:return False
            old=json.loads(row[3])
            evidence=evidence_for(r,SimpleNamespace(status="PERSISTENT"),quality.get("score"))
            evidence["neighbor_observed_at"]=at
            evidence["neighbor_evidence"]=[{k:x[k] for k in ("ap_name","band","channel","width","snr","discovery","neighbor_at","target_radio_at","relation","overlap_mhz","candidate","confidence")} for x in matches]
            for code in sorted({x["candidate"] for x in matches}):
                evidence["candidates"].append({"code":code,"label":"Aligned neighbor contention candidate",
                    "confidence":"medium","basis":["verified_receiving_radio_scope","direct_ota_observation",
                        "explicit_channel_block_overlap","aligned_persistent_congestion"],
                    "limit":"Correlation only; actual cause, client impact and service outage unverified"})
            if old==evidence:return False
            rev=row[2]+1;payload=json.dumps(evidence,sort_keys=True,allow_nan=False)
            c.execute("UPDATE rf_incidents SET evidence_rev=?,evidence=? WHERE incident_id=?",(rev,payload,ident))
            c.execute("INSERT INTO rf_incident_events VALUES(?,?,?,?,?)",(ident,rev,at,"PERSISTENT",payload))
            return True
    def current_id(self,c,radio_id,context):
        row=c.execute("""SELECT incident_id FROM rf_incidents WHERE radio_id=? AND context=? AND ended_at IS NULL""",
            (radio_id,context)).fetchone()
        return row[0] if row else None
    def list(self,page=1,status="all",start=None,end=None,size=50,current=()):
        if type(page) is not int or not 1<=page<=10000 or not 1<=size<=100 or status not in ("all","open","closed"):
            raise ValueError("invalid incident filter")
        end=int(self.clock())+1 if end is None else end
        if start is None:start=max(0,end-365*86400)
        if type(start) is not int or type(end) is not int or not 0<=start<end or end-start>366*86400:
            raise ValueError("invalid incident time range")
        clause="last_at>=? AND first_at<?"
        if status=="open":clause+=" AND ended_at IS NULL"
        if status=="closed":clause+=" AND ended_at IS NOT NULL"
        # No internal MAC or context values leave this method.
        with self.transaction() as c:
            total=c.execute("SELECT COUNT(*) FROM rf_incidents WHERE "+clause,(start,end)).fetchone()[0]
            rows=c.execute("""SELECT incident_id,ap_name,controller,band,first_at,last_at,persistent_at,ended_at,state,evidence_rev,evidence
                FROM rf_incidents WHERE """+clause+" ORDER BY last_at DESC,incident_id LIMIT ? OFFSET ?",
                (start,end,size,(page-1)*size)).fetchall()
            result=[]
            for row in rows:
                item=dict(zip(("incident_id","ap_name","controller","band","first_at","last_at","persistent_at","ended_at","state","revision","evidence"),row))
                item["evidence"]=json.loads(item["evidence"])
                item["current_observation"]=item["incident_id"] in current and item["ended_at"] is None and item["state"]!="UNCONFIRMED"
                item["display_state"]="UNCONFIRMED" if item["ended_at"] is None and not item["current_observation"] else item["state"]
                review=c.execute("""SELECT verdict,reason,candidate,at,evidence_rev FROM rf_incident_reviews
                    WHERE incident_id=? ORDER BY at DESC,rowid DESC LIMIT 1""",(item["incident_id"],)).fetchone()
                item["review"]=dict(zip(("verdict","reason","candidate","at","revision"),review)) if review else None
                item["review_current"]=bool(review and review[4]==item["revision"])
                result.append(item)
        return {"rows":result,"total":total,"page":page,"size":size,"scope":"locally_attested_radio","service_impact":"unverified"}
    def detail(self,ident):
        if not isinstance(ident,str) or not IDENT.fullmatch(ident):raise ValueError("invalid incident ID")
        with self.transaction() as c:
            row=c.execute("""SELECT incident_id,ap_name,controller,band,first_at,last_at,persistent_at,ended_at,state,evidence_rev,evidence
                FROM rf_incidents WHERE incident_id=?""",(ident,)).fetchone()
            if row is None:raise ValueError("unknown incident")
            item=dict(zip(("incident_id","ap_name","controller","band","first_at","last_at","persistent_at","ended_at","state","revision","evidence"),row))
            item["evidence"]=json.loads(item["evidence"])
            events=c.execute("""SELECT revision,at,state,evidence FROM rf_incident_events WHERE incident_id=?
                ORDER BY revision DESC LIMIT 100""",(ident,)).fetchall()
            item["timeline"]=[{"revision":r,"at":t,"state":s,"evidence":json.loads(e)} for r,t,s,e in reversed(events)]
            item["timeline_truncated"]=item["revision"]>len(events)
            reviews=c.execute("""SELECT evidence_rev,at,verdict,reason,candidate FROM rf_incident_reviews
                WHERE incident_id=? ORDER BY at DESC,rowid DESC LIMIT 100""",(ident,)).fetchall()
            item["reviews"]=[dict(zip(("revision","at","verdict","reason","candidate"),r)) for r in reviews]
            item["reviews_truncated"]=c.execute("SELECT COUNT(*) FROM rf_incident_reviews WHERE incident_id=?",(ident,)).fetchone()[0]>len(reviews)
            item["service_impact"]="unverified"
            return item
    def review(self,ident,revision,request_id,user,verdict,reason,candidate):
        if (not isinstance(ident,str) or not IDENT.fullmatch(ident) or type(revision) is not int or revision<1
            or not isinstance(request_id,str) or not REQUEST.fullmatch(request_id)
            or not isinstance(user,str) or not USER.fullmatch(user)
            or verdict not in REVIEW_VERDICTS or reason not in REASONS or candidate not in ("RF04","RF05","RF06","UNKNOWN")):
            raise ValueError("invalid review")
        if candidate=="UNKNOWN" and verdict=="supported":raise ValueError("unknown cause cannot be confirmed")
        with self.transaction() as c:
            # Scope idempotency to the authenticated reviewer, not a global
            # user-supplied ID that could collide with another user.
            key=user+":"+request_id
            existing=c.execute("""SELECT incident_id,evidence_rev,username,verdict,reason,candidate
                FROM rf_incident_reviews WHERE request_id=?""",(key,)).fetchone()
            if existing:
                if existing!=(ident,revision,user,verdict,reason,candidate):raise ValueError("request ID conflict")
                return {"incident_id":ident,"revision":revision,"recorded":True,"duplicate":True}
            # Historical evidence can be reviewed explicitly, including after
            # recovery. Such a review never counts as review of a newer revision.
            row=c.execute("SELECT evidence FROM rf_incident_events WHERE incident_id=? AND revision=?",(ident,revision)).fetchone()
            if row is None:raise ReviewConflict("evidence revision unavailable")
            if candidate not in {x["code"] for x in json.loads(row[0])["candidates"]}:raise ValueError("candidate not in evidence")
            c.execute("INSERT INTO rf_incident_reviews VALUES(?,?,?,?,?,?,?,?)",
                (key,ident,revision,user,int(self.clock()),verdict,reason,candidate))
            return {"incident_id":ident,"revision":revision,"recorded":True,"duplicate":False}

class ReviewConflict(RuntimeError):pass
