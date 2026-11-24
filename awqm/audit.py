"""Local durable authentication and diagnostic access audit.

Only fixed event names, approved existing IDs and logical target identifiers
are recorded. No request bodies, tokens, passwords, raw CLI or exception text.
Audit storage errors fail closed for authentication and report access.
"""
import sqlite3,time
from pathlib import Path
from threading import RLock
from contextlib import contextmanager

EVENTS=frozenset(("login","denied","logout","history","export_csv","export_html","admin_status","incident_detail","client_view","incident_summary","incident_csv","incident_html","neighbor_view","association_history"))

class AuditLog:
    def __init__(self,path,clock=time.time):
        self.path=Path(path);self.clock=clock;self.lock=RLock()
        with self._db() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS audit(
                id INTEGER PRIMARY KEY, at INTEGER NOT NULL,
                username TEXT, event TEXT NOT NULL, target TEXT)""")
            c.execute("CREATE INDEX IF NOT EXISTS audit_at ON audit(at)")
    @contextmanager
    def _db(self):
        with self.lock:
            c=sqlite3.connect(self.path,timeout=2)
            try:
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("PRAGMA synchronous=FULL")
                with c:yield c
            finally:c.close()
    def record(self,username,event,target=None):
        if event not in EVENTS or username is not None and (not isinstance(username,str) or len(username)>128):
            raise ValueError("invalid audit event")
        if target is not None and (not isinstance(target,str) or len(target)>128):
            raise ValueError("invalid audit target")
        with self._db() as c:
            c.execute("INSERT INTO audit(at,username,event,target) VALUES(?,?,?,?)",
                      (int(self.clock()),username,event,target))
    def auth_event(self,entry):
        self.record(entry["username"],entry["event"])
    def summary(self):
        with self._db() as c:
            n,at=c.execute("SELECT COUNT(*),MAX(at) FROM audit").fetchone()
            return {"events":n,"last_event_at":at}
