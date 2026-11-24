"""Durable, revisioned collection-only maintenance control.

Only the coordinator applies a desired state. Acceptance never means an
in-flight query has drained. Pause survives process restarts. No automatic
resume, command execution or device configuration input.
"""
from contextlib import contextmanager
from pathlib import Path
import hashlib,json,sqlite3,time,os
from .service_config import safe_path,ID

class OperationConflict(RuntimeError):pass
class OperationRejected(ValueError):pass

def configured_ops(cfg):
    binding=hashlib.sha256(json.dumps([cfg.site,sorted((t.id,t.ip) for t in cfg.targets)],
        separators=(",",":")).encode("utf-8")).hexdigest()
    # A retained marker distinguishes first installation from accidental
    # deletion of an established PM database while the service is stopped.
    path=safe_path(cfg.root,"config/operations.db")
    marker=safe_path(cfg.root,"config/operations.initialized")
    if marker.exists() and not path.is_file():raise ValueError("operation database missing")
    ops=Operations(path,tuple(t.id for t in cfg.targets),binding)
    if marker.exists():
        if marker.read_text(encoding="ascii")!="AWQM-operations-v1\n":raise ValueError("operation marker invalid")
    else:
        try:
            with marker.open("x",encoding="ascii",newline="\n") as f:
                f.write("AWQM-operations-v1\n");f.flush();os.fsync(f.fileno())
        except FileExistsError:
            if marker.read_text(encoding="ascii")!="AWQM-operations-v1\n":raise ValueError("operation marker invalid")
    return ops

class Operations:
    def __init__(self,path,controllers,binding="fixture",clock=time.time):
        self.path=Path(path);self.controllers=tuple(controllers);self.binding=binding;self.clock=clock
        if not 1<=len(self.controllers)<=4 or len(set(self.controllers))!=len(self.controllers) or any(not ID.fullmatch(k) or k=="all" for k in self.controllers):
            raise ValueError("invalid controller configuration")
        with self._db(write=True) as c:
            c.execute("CREATE TABLE IF NOT EXISTS ops_meta(id INTEGER PRIMARY KEY CHECK(id=1),binding TEXT NOT NULL,revision INTEGER NOT NULL)")
            c.execute("""CREATE TABLE IF NOT EXISTS ops_state(controller TEXT PRIMARY KEY,paused INTEGER NOT NULL CHECK(paused IN (0,1)),
                      revision INTEGER NOT NULL,changed_at INTEGER NOT NULL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS ops_events(revision INTEGER PRIMARY KEY,at INTEGER NOT NULL,actor TEXT NOT NULL,
                      target TEXT NOT NULL,paused INTEGER NOT NULL)""")
            row=c.execute("SELECT binding FROM ops_meta WHERE id=1").fetchone()
            if row is None:
                if c.execute("SELECT COUNT(*) FROM ops_state").fetchone()[0]:raise ValueError("operation state inconsistent")
                c.execute("INSERT INTO ops_meta VALUES(1,?,0)",(binding,))
                c.executemany("INSERT INTO ops_state VALUES(?,0,0,?)",((k,int(self.clock())) for k in self.controllers))
            self._read(c)
    @contextmanager
    def _db(self,write=False):
        c=sqlite3.connect(self.path,timeout=2)
        try:
            if write:
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("PRAGMA synchronous=FULL")
            c.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield c
            c.commit()
        except BaseException:
            c.rollback();raise
        finally:c.close()
    def _read(self,c):
        row=c.execute("SELECT binding,revision FROM ops_meta WHERE id=1").fetchone()
        states={k:{"requested_paused":bool(p),"revision":v,"changed_at":t} for k,p,v,t in
                c.execute("SELECT controller,paused,revision,changed_at FROM ops_state")}
        if not row or row[0]!=self.binding or row[1]<0 or set(states)!=set(self.controllers) or any(not 0<=s["revision"]<=row[1] for s in states.values()):
            raise ValueError("operation configuration changed; local reconciliation required")
        return {"revision":row[1],"controllers":states}
    def snapshot(self):
        if not self.path.is_file():raise RuntimeError("operation state missing")
        with self._db() as c:return self._read(c)
    def paused(self,controller):
        return self.snapshot()["controllers"][controller]["requested_paused"]
    def set(self,actor,target,paused,expected_revision):
        if target!="all" and target not in self.controllers:raise OperationRejected("unknown_controller")
        if type(paused) is not bool or type(expected_revision) is not int or expected_revision<0:raise OperationRejected("invalid_operation")
        if not isinstance(actor,str) or not 1<=len(actor)<=150 or any(ord(ch)<32 for ch in actor):raise OperationRejected("invalid_actor")
        with self._db(write=True) as c:
            state=self._read(c)
            if state["revision"]!=expected_revision:raise OperationConflict("operation_revision_changed")
            targets=self.controllers if target=="all" else (target,)
            changed=[k for k in targets if state["controllers"][k]["requested_paused"]!=paused]
            if not changed:return {**state,"changed":False,"accepted":True}
            revision=state["revision"]+1;at=int(self.clock())
            c.execute("UPDATE ops_meta SET revision=? WHERE id=1",(revision,))
            c.executemany("UPDATE ops_state SET paused=?,revision=?,changed_at=? WHERE controller=?",((int(paused),revision,at,k) for k in changed))
            c.execute("INSERT INTO ops_events VALUES(?,?,?,?,?)",(revision,at,actor,target,int(paused)))
            return {**self._read(c),"changed":True,"accepted":True}
    def recent(self):
        with self._db() as c:
            self._read(c)
            return [dict(revision=v,at=t,target=k,requested_paused=bool(p)) for v,t,k,p in
                    c.execute("SELECT revision,at,target,paused FROM ops_events ORDER BY revision DESC LIMIT 100")]
