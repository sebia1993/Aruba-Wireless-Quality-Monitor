import unittest
from awqm.selection import Selector
from awqm.jobs import Jobs

class Worker:
    def __init__(self):self.jobs=Jobs();self.now=120
    def submit_detail(self,req,name,target,ttl):return self.jobs.submit(req,name,target,self.now,ttl)
def row(i,**kw):
    r=dict(source="synthetic",status="PERSISTENT",open_incident=True,complete=True,
        observed_at=120,controller="w",score=40,clients=i,ap_id=f"a{i}",ap_name=f"AP{i}")
    r.update(kw);return r
class SelectionTests(unittest.TestCase):
    def test_cap_fresh_and_dedupe(self):
        w=Worker();s=Selector()
        rows=[row(i) for i in range(10)]+[row(9,score=20),row(11,source="aos8-cli-unvalidated"),
            row(12,status="UNKNOWN"),row(13,observed_at=10)]
        self.assertEqual(len(s.plan(rows,120,{"w":w})),2)
        self.assertEqual({j.target for j in w.jobs.pending(120)},{"AP9","AP8"})
        self.assertEqual(len(s.plan(rows,120,{"w":w})),2)
        self.assertEqual(len(s.plan(rows,120,{"w":w})),1)
        self.assertEqual(len(s.plan(rows,120,{"w":w})),0)
        self.assertEqual(len(w.jobs.pending(120)),5)
