"""Durable initial common RF incident evidence, not a service outage.

Only the coordinator's validated CL01 review is an admissible input. This
storage prerequisite is not yet enabled in the live coordinator: retention
of parent lifecycles and operational presentation need their own integration.
"""
import json
from uuid import uuid4
from .incidents import IDENT
from .controller_load_review import review_common_groups

TABLES={"rf_common_incidents","rf_common_incident_children"}
MAX_PAIRS=100
MAX_INT=2**63-1

def _integer(value):
    return type(value) is int and 0<=value<=MAX_INT

def schema(c):
    found={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not found & TABLES:return False
    if not TABLES<=found:raise ValueError("common incident schema incomplete")
    return True

def _ident(value):
    return isinstance(value,str) and IDENT.fullmatch(value) is not None

def _pairs_valid(pairs,radio_at):
    if not isinstance(pairs,list) or not 1<=len(pairs)<=MAX_PAIRS:return False
    seen=set()
    for p in pairs:
        if not isinstance(p,dict) or set(p)!={"incident_ids","rf_candidate","overlap_mhz","window_start","window_end"}:return False
        ids=p["incident_ids"]
        if (not isinstance(ids,list) or len(ids)!=2 or not all(_ident(i) for i in ids)
                or ids[0]>=ids[1] or tuple(ids) in seen
                or p["rf_candidate"] not in ("RF05","RF06")
                or not _integer(p["overlap_mhz"]) or not 0<p["overlap_mhz"]<=160
                or not _integer(p["window_start"]) or not _integer(p["window_end"])
                or not p["window_start"]<=radio_at<=p["window_end"]
                or p["window_end"]-p["window_start"]>30):return False
        seen.add(tuple(ids))
    return True

def _resource_window_valid(evidence,radio_at,pairs):
    """Same bounded resource contract for capture and historical reads."""
    times=[evidence.get("cpu_at"),evidence.get("datapath_at"),radio_at]
    if not all(_integer(t) for t in times):return False
    if any(not p["window_start"]<=min(times)<=max(times)<=p["window_end"]
           for p in pairs):return False
    resources=[evidence.get(k) for k in
               ("cpu_percent","datapath_max_1s_percent","datapath_max_4s_percent")]
    return (all(type(v) in (int,float) and 0<=v<=100 for v in resources)
            and resources[0]>=70)

def _child_identity_valid(rows):
    """The original group must still identify distinct same-site APs."""
    if any(not isinstance(r[0],str) or r[0].count(":")<2
           or not _integer(r[1]) or not isinstance(r[4],str) or not r[4]
           for r in rows):return False
    physical=[r[0].rsplit(":",1)[0] for r in rows]
    return (len(set(physical))==len(rows)
            and len({p.split(":",1)[0] for p in physical})==1
            and len({r[4] for r in rows})==1
            and max(r[1] for r in rows)-min(r[1] for r in rows)<=30)

def _revision(c,ident,revision,radio_at,window_end):
    # Neighbor evidence is appended at its own observation time, which
    # can be later than the underlying Radio sample. Bind the immutable
    # revision to that exact Radio sample and the validated group window,
    # not to an impossible requirement that the neighbor event predate it.
    row=c.execute("""SELECT e.evidence FROM rf_incidents i JOIN rf_incident_events e
        ON e.incident_id=i.incident_id WHERE i.incident_id=? AND i.evidence_rev>=?
        AND e.revision=? AND e.state='PERSISTENT' AND e.at>=? AND e.at<=?
        AND i.first_at<=? AND i.last_at>=?
        AND (i.evidence_rev>? OR i.evidence=e.evidence)""",
        (ident,revision,revision,radio_at,window_end,radio_at,radio_at,revision)).fetchone()
    if row is None:return False
    try:e=json.loads(row[0])
    except (ValueError,TypeError,RecursionError):return False
    return (isinstance(e,dict) and _integer(e.get("observed_at"))
            and e["observed_at"]==radio_at and e.get("risk_status")=="PERSISTENT")

class CommonIncidents:
    def __init__(self,store):
        if store.mode!="diagnostic":raise ValueError("diagnostic store required")
        self.store=store
        with store.lock,store._db(store.root/"core.db") as c:
            schema(c)
            c.executescript("""
            CREATE TABLE IF NOT EXISTS rf_common_incidents(
              parent_id TEXT PRIMARY KEY, radio_at INTEGER NOT NULL,
              window_start INTEGER NOT NULL, window_end INTEGER NOT NULL,
              evidence TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS rf_common_incident_children(
              child_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL,
              evidence_rev INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS rf_common_parent_children
              ON rf_common_incident_children(parent_id,child_id);
            """)

    def capture(self,controller,review,now):
        """Freeze first complete evidence and exact child revisions atomically.

        One immutable parent per disjoint child set. Never attach a child
        to a different parent, update its state, or treat missing data as
        recovery. Subsequent valid observations of the same group return
        the existing parent without rewriting its original evidence.
        """
        if (not _integer(now) or review.get("status")!="review_candidate"
                or review.get("code")!="CL01" or review.get("verified") is not False
                or review.get("service_impact")!="unverified"):return []
        radio_at=review.get("radio_at")
        common=review.get("common_incident_review",{})
        if (not _integer(radio_at) or not 0<=now-radio_at<=90
                or not _integer(review.get("valid_until")) or now>=review["valid_until"]
                or common.get("status")!="review_pairs" or common.get("verified") is not False
                or common.get("service_impact")!="unverified"
                or not _integer(common.get("valid_until")) or now>=common["valid_until"]):return []
        pairs=common.get("pairs")
        if not _pairs_valid(pairs,radio_at):return []
        group_review=review_common_groups(pairs)
        if group_review!=common.get("group_review"):return []
        times=[review.get(k) for k in ("cpu_at","datapath_at","radio_at")]
        if not all(_integer(t) and 0<=now-t<=90 for t in times):return []
        if not _resource_window_valid(review,radio_at,pairs):return []
        if any(not 0<=now-p["window_end"]<=90 or now-p["window_start"]>90 for p in pairs):return []
        resources={k:review.get(k) for k in ("cpu_percent","datapath_max_1s_percent","datapath_max_4s_percent")}
        parents=[]
        with self.store.lock,self.store._db(self.store.root/"core.db") as c:
            for group in group_review["groups"]:
                ids=group["incident_ids"]
                rows=[c.execute("""SELECT radio_id,first_at,last_at,evidence_rev,controller,state,ended_at
                    FROM rf_incidents WHERE incident_id=?""",(i,)).fetchone() for i in ids]
                if any(r is None or r[2]!=radio_at or r[4]!=controller or r[5]!="PERSISTENT"
                       or r[6] is not None or not _revision(c,i,r[3],radio_at,group["window_end"]) for i,r in zip(ids,rows)):continue
                if not _child_identity_valid(rows):continue
                existing={r[0] for i in ids for r in c.execute(
                    "SELECT parent_id FROM rf_common_incident_children WHERE child_id=?",(i,))}
                if existing:
                    if len(existing)==1:
                        parent=next(iter(existing))
                        saved=[r[0] for r in c.execute("""SELECT child_id FROM rf_common_incident_children
                            WHERE parent_id=? ORDER BY child_id""",(parent,))]
                        if saved==ids:
                            row=c.execute("SELECT * FROM rf_common_incidents WHERE parent_id=?",(parent,)).fetchone()
                            if row is not None:
                                try:_validated_parent(c,row)
                                except ValueError:continue
                                parents.append(parent)
                    continue
                parent=uuid4().hex
                links=[p for p in pairs if set(p["incident_ids"])<=set(ids)]
                evidence={"version":1,"confidence":"low","service_impact":"unverified",
                          "verified":False,"pairs":links,"cpu_at":times[0],"datapath_at":times[1],
                          **resources,"children":[[i,r[3]] for i,r in zip(ids,rows)]}
                c.execute("INSERT INTO rf_common_incidents VALUES(?,?,?,?,?)",
                          (parent,radio_at,group["window_start"],group["window_end"],
                           json.dumps(evidence,sort_keys=True,allow_nan=False)))
                c.executemany("INSERT INTO rf_common_incident_children VALUES(?,?,?)",
                              [(i,parent,r[3]) for i,r in zip(ids,rows)])
                parents.append(parent)
        return parents

def validate_links(c):
    """Read-only logical validation of copied core, not just file integrity."""
    if not schema(c):return
    if c.execute("""SELECT 1 FROM rf_common_incident_children ch
        LEFT JOIN rf_common_incidents p ON p.parent_id=ch.parent_id
        WHERE p.parent_id IS NULL LIMIT 1""").fetchone():raise ValueError("common incident parent unavailable")
    for row in c.execute("SELECT * FROM rf_common_incidents"):
        _validated_parent(c,row)

def _validated_parent(c,row):
    parent,at,start,end,payload=row
    try:e=json.loads(payload)
    except (ValueError,TypeError,RecursionError) as exc:raise ValueError("common incident evidence invalid") from exc
    if (not _ident(parent) or not _integer(at) or not isinstance(e,dict)
            or e.get("version")!=1 or e.get("confidence")!="low" or e.get("verified") is not False
            or e.get("service_impact")!="unverified" or not _pairs_valid(e.get("pairs"),at)):
        raise ValueError("common incident evidence invalid")
    if not _resource_window_valid(e,at,e["pairs"]):
        raise ValueError("common incident resource evidence invalid")
    groups=review_common_groups(e["pairs"])
    if groups["withheld_components"] or len(groups["groups"])!=1:raise ValueError("common incident group invalid")
    g=groups["groups"][0]
    children=[list(r) for r in c.execute("""SELECT child_id,evidence_rev FROM rf_common_incident_children
        WHERE parent_id=? ORDER BY child_id LIMIT 201""",(parent,))]
    if ([i for i,_ in children]!=g["incident_ids"] or children!=e.get("children")
            or (start,end)!=(g["window_start"],g["window_end"])):
        raise ValueError("common incident membership mismatch")
    if any(not _integer(rev) or rev<1 or not _revision(c,i,rev,at,end) for i,rev in children):
        raise ValueError("common incident child revision unavailable")
    rows=[c.execute("""SELECT radio_id,first_at,last_at,evidence_rev,controller
        FROM rf_incidents WHERE incident_id=?""",(i,)).fetchone() for i,_ in children]
    if any(r is None for r in rows) or not _child_identity_valid(rows):
        raise ValueError("common incident child identity mismatch")
    return children

def lifecycle_for_child(c,ident):
    """Historical resolution evidence for one immutable group, not live RF state.

    Read under the existing incident transaction. Do not close/reassign a
    parent or change retention; a missing or invalid event cannot prove recovery.
    """
    if not schema(c):return None
    link=c.execute("SELECT parent_id FROM rf_common_incident_children WHERE child_id=?",
                   (ident,)).fetchone()
    if link is None:return None
    parent=link[0]
    result={"parent_id":parent if _ident(parent) else None,
            "status":"unavailable","child_count":None,"resolved_count":None,
            "first_resolved_at":None,"last_resolved_at":None,
            "service_impact":"unverified","verified":False}
    row=c.execute("SELECT * FROM rf_common_incidents WHERE parent_id=?",(parent,)).fetchone()
    if row is None:return result
    try:children=_validated_parent(c,row)
    except ValueError:return result
    resolved=[]
    for child,initial_revision in children:
        snapshot=c.execute("""SELECT state,ended_at,last_at,evidence_rev,evidence
            FROM rf_incidents WHERE incident_id=?""",(child,)).fetchone()
        if snapshot is None:return result
        state,end,last,revision,payload=snapshot
        if state!="RESOLVED":continue
        if (not _integer(end) or end<=row[3] or end!=last or
                not _integer(revision) or revision<=initial_revision):return result
        event=c.execute("""SELECT at,state,evidence FROM rf_incident_events
            WHERE incident_id=? AND revision=?""",(child,revision)).fetchone()
        if event!=(end,"RESOLVED",payload):return result
        try:e=json.loads(payload)
        except (ValueError,TypeError,RecursionError):return result
        if (not isinstance(e,dict) or not _integer(e.get("observed_at"))
                or e["observed_at"]!=end or e.get("risk_status")!="RESOLVED"
                or e.get("service_impact")!="unverified"):return result
        resolved.append(end)
    result.update(status=("all_children_resolved_observed" if len(resolved)==len(children)
                          else "not_all_resolved_observed"),
                  child_count=len(children),resolved_count=len(resolved),
                  first_resolved_at=min(resolved) if resolved else None,
                  last_resolved_at=max(resolved) if resolved else None)
    return result


def held_children(c,ids):
    """Until parent lifecycle is implemented, never retire referenced children."""
    if not schema(c):return set()
    return {i for i in ids if c.execute(
        "SELECT 1 FROM rf_common_incident_children WHERE child_id=?",(i,)).fetchone()}

def backup_matches(c,backup,ident):
    """A raw day with a grouped child needs its exact immutable parent copy."""
    if not schema(c):return True
    child=c.execute("SELECT * FROM rf_common_incident_children WHERE child_id=?",(ident,)).fetchone()
    if child is None:return True
    if not schema(backup):return False
    parent=child[1]
    for query in ("SELECT * FROM rf_common_incidents WHERE parent_id=?",
                  "SELECT * FROM rf_common_incident_children WHERE parent_id=? ORDER BY child_id"):
        a=c.execute(query,(parent,)).fetchall()
        b=backup.execute(query,(parent,)).fetchall()
        if not a or a!=b:return False
    # Matching bytes alone do not make an invalid common incident safe.
    # Validate only the referenced parent, not the entire archive ledger.
    for db in (c,backup):
        row=db.execute("SELECT * FROM rf_common_incidents WHERE parent_id=?",(parent,)).fetchone()
        try:_validated_parent(db,row)
        except ValueError:return False
    return True
