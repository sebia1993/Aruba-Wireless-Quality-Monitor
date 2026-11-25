"""Transactional durable job receipts.

SQLite is independent from telemetry partitions. Job deadlines use epoch time,
not process-local monotonic time. A running job found at startup is failed with
unknown outcome and never replayed automatically. A still-valid queued job must
pass fresh target and safety checks before execution.
"""
import json,sqlite3,time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from threading import RLock
from .jobs import Jobs,Job,TERMINAL

class DurableJobs:
    def __init__(self,path,scope,now=None,limit=20,history_limit=500):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.scope=scope;self.limit=limit;self.history_limit=history_limit;self.lock=RLock()
        with self._db() as c:
            c.execute("CREATE TABLE IF NOT EXISTS queues(scope TEXT PRIMARY KEY,state TEXT NOT NULL)")
        now=time.time() if now is None else now
        def recover(q):
            for j in list(q.records.values()):
                if j.status=="running":q.finish(j.job_id,"failed","interrupted_outcome_unknown",now)
                elif j.status in ("queued","deferred"):q.defer(j.job_id,"restart_revalidate")
            q.pending(now)
        self._apply(recover)

    @contextmanager
    def _db(self):
        c=sqlite3.connect(self.path,timeout=5)
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=FULL")
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:c.close()

    def _apply(self,fn):
        with self.lock,self._db() as c:
            q=Jobs(self.limit,self.history_limit)
            old=c.execute("SELECT state FROM queues WHERE scope=?",(self.scope,)).fetchone()
            if old:
                data=json.loads(old[0])
                if data.get("version")!=1:raise ValueError("unsupported queue schema")
                q.records=OrderedDict((j["job_id"],Job(**j)) for j in data["records"])
                q.requests=dict(data["requests"])
                q.active={(j.name,j.target):j.job_id for j in q.records.values() if j.status not in TERMINAL}
                if any(k not in q.records for k in q.requests.values()):
                    raise ValueError("invalid receipt index")
            result=fn(q)
            state=json.dumps({"version":1,"records":[asdict(j) for j in q.records.values()],
                              "requests":q.requests},ensure_ascii=False,allow_nan=False,separators=(",",":"))
            if not old or state!=old[0]:
                c.execute("INSERT INTO queues VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET state=excluded.state",(self.scope,state))
            return result

    def submit(self,*a,**kw):return self._apply(lambda q:q.submit(*a,**kw))
    def get(self,*a):return self._apply(lambda q:q.get(*a))
    def by_request(self,*a):return self._apply(lambda q:q.by_request(*a))
    def pending(self,*a):return self._apply(lambda q:q.pending(*a))
    def defer(self,*a):return self._apply(lambda q:q.defer(*a))
    def start(self,*a):return self._apply(lambda q:q.start(*a))
    def finish(self,*a):return self._apply(lambda q:q.finish(*a))
    def cancel(self,*a):return self._apply(lambda q:q.cancel(*a))
    def list(self,*a):return self._apply(lambda q:q.list(*a))
