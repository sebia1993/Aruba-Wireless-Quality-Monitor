"""Persist locally-attested Radio quality separately from raw telemetry.

Never changes the diagnostic provenance of raw data. No client cause or service
impact is inferred. Only a current, matched, locally approved Radio observation
can be published as a policy evaluation.
"""
import json
from dataclasses import asdict,replace
from .analysis import State,advance,radio_score

class QualityLedger:
    def __init__(self,store):
        if store.mode!="diagnostic":raise ValueError("diagnostic store required")
        self.store=store;self.cache={}
        with store.lock,store._db(store.root/"core.db") as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS radio_quality_latest(
              radio_id TEXT PRIMARY KEY, observed_at INTEGER NOT NULL, approval TEXT NOT NULL,
              context TEXT NOT NULL, state TEXT NOT NULL, score INTEGER);
            CREATE TABLE IF NOT EXISTS radio_quality_events(
              radio_id TEXT NOT NULL, observed_at INTEGER NOT NULL, approval TEXT NOT NULL,
              context TEXT NOT NULL, status TEXT NOT NULL, score INTEGER,
              PRIMARY KEY(radio_id,observed_at,approval,context,status));
            """)
    def evaluate(self,radios,snapshots,now,committed):
        """Single writer, idempotent; only already committed current samples."""
        by={s.controller:s for s in snapshots}
        results={};todo=[]
        for r in radios:
            s=by.get(r.controller)
            if (not s or not s.field_tag or not s.field_until or now>=s.field_until
                or r.observed_at!=s.observed_at or committed.get(r.radio_id)!=r.observed_at):
                continue
            if r.source!="aos8-cli-unvalidated":continue
            context=s.field_tag+"|"+s.field_connection+"|"+r.epoch
            cached=self.cache.get(r.radio_id)
            if cached and cached[0]==context and cached[1]["observed_at"]==r.observed_at:
                results[r.radio_id]=(context,dict(cached[1],valid_until=s.field_until))
            else:todo.append((r,s,context))
        # No SQLite reads or writes for repeated unchanged telemetry.
        if not todo:
            self.cache=results
            return {k:v[1] for k,v in results.items()}
        with self.store.lock,self.store._db(self.store.root/"core.db") as c:
            for r,s,context in todo:
                # Protect against a stale commit after identity or firmware changes.
                latest=c.execute("SELECT observed_at FROM latest WHERE radio_id=?",(r.radio_id,)).fetchone()
                if latest!=(r.observed_at,):continue
                old=c.execute("SELECT observed_at,approval,context,state,score FROM radio_quality_latest WHERE radio_id=?",(r.radio_id,)).fetchone()
                if old and old[0]>r.observed_at:continue
                if old and old[0]==r.observed_at:
                    if old[1]!=s.field_tag or old[2]!=context:continue
                    state=State(**json.loads(old[3]));score=old[4]
                else:
                    previous=State(**json.loads(old[3])) if old and old[1]==s.field_tag and old[2]==context else State(status="UNKNOWN")
                    # A new validation/connection context starts a new evidence
                    # sequence. The prior event is retained, not marked recovered.
                    row=replace(r,epoch=context)
                    state=advance(previous,row,max_gap=270 if s.field_profile=="safe" else 90)
                    score=radio_score(row)
                    c.execute("""INSERT INTO radio_quality_latest VALUES(?,?,?,?,?,?)
                        ON CONFLICT(radio_id) DO UPDATE SET observed_at=excluded.observed_at,
                        approval=excluded.approval,context=excluded.context,state=excluded.state,score=excluded.score""",
                        (r.radio_id,r.observed_at,s.field_tag,context,json.dumps(asdict(state)),score))
                    if old is None or old[2]!=context or previous.status!=state.status:
                        c.execute("INSERT OR IGNORE INTO radio_quality_events VALUES(?,?,?,?,?,?)",
                            (r.radio_id,r.observed_at,s.field_tag,context,state.status,score))
                results[r.radio_id]=(context,{"observed_at":r.observed_at,"score":score,
                    "status":state.status,"approval":s.field_tag[:12],"valid_until":s.field_until,
                    "policy":"balanced-radio-v1","scope":"local_operator_attested_radio"})
        self.cache=results
        return {k:v[1] for k,v in results.items()}
