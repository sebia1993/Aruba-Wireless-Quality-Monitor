"""Local SQLite development storage. Cross-partition spool/retention pending."""
import json, sqlite3
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import RLock
from .model import Radio
from .analysis import State, advance, radio_score

class Store:
    def __init__(self,root):
        self.root=Path(root)
        self.root.mkdir(parents=True,exist_ok=True)
        self.lock=RLock()
        with self._db(self.root/"core.db") as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS latest(radio_id TEXT PRIMARY KEY,observed_at INTEGER NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS transitions(radio_id TEXT,observed_at INTEGER,status TEXT,PRIMARY KEY(radio_id,observed_at,status));
            """)
    def _db(self,path):
        # sqlite context manager handles commit/rollback; closing is explicit at call sites.
        from contextlib import contextmanager
        @contextmanager
        def connection():
            c=sqlite3.connect(path,timeout=5)
            try:
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("PRAGMA synchronous=FULL")
                with c:
                    yield c
            finally:
                c.close()
        return connection()
    def save(self, row:Radio):
        payload=json.dumps(asdict(row),ensure_ascii=False,allow_nan=False)
        date=datetime.fromtimestamp(row.observed_at,timezone(timedelta(hours=9))).strftime("%Y%m%d")
        with self.lock:
            # Write raw first. Idempotent replay recovers a crash before updating latest.
            with self._db(self.root/f"radio_{date}.db") as c:
                c.execute("CREATE TABLE IF NOT EXISTS radio_raw(radio_id TEXT,observed_at INTEGER,payload TEXT,PRIMARY KEY(radio_id,observed_at))")
                c.execute("INSERT OR IGNORE INTO radio_raw VALUES(?,?,?)",(row.radio_id,row.observed_at,payload))
                actual=c.execute("SELECT payload FROM radio_raw WHERE radio_id=? AND observed_at=?",(row.radio_id,row.observed_at)).fetchone()[0]
                if actual!=payload:
                    raise ValueError("conflicting sample")
            with self._db(self.root/"core.db") as c:
                old=c.execute("SELECT observed_at,state FROM latest WHERE radio_id=?",(row.radio_id,)).fetchone()
                if old and old[0]>=row.observed_at:
                    return False
                previous=State(**json.loads(old[1])) if old else State()
                state=advance(previous,row)
                c.execute("INSERT INTO latest VALUES(?,?,?,?) ON CONFLICT(radio_id) DO UPDATE SET observed_at=excluded.observed_at,payload=excluded.payload,state=excluded.state",
                          (row.radio_id,row.observed_at,payload,json.dumps(asdict(state))))
                if previous.status!=state.status:
                    c.execute("INSERT OR IGNORE INTO transitions VALUES(?,?,?)",(row.radio_id,row.observed_at,state.status))
                return True
    def snapshot(self):
        with self.lock, self._db(self.root/"core.db") as c:
            data=c.execute("SELECT payload,state FROM latest ORDER BY radio_id").fetchall()
            events=c.execute("SELECT radio_id,observed_at,status FROM transitions ORDER BY observed_at DESC LIMIT 100").fetchall()
        rows=[]
        for p,s in data:
            row=Radio(**json.loads(p))
            rows.append({**asdict(row),"score":radio_score(row),**json.loads(s)})
        return {"mode":"demo","radios":rows,"events":[dict(zip(("radio_id","observed_at","status"),v)) for v in events]}
