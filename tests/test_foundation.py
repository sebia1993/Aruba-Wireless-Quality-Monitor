import json, tempfile, unittest
from dataclasses import asdict, replace
from awqm.model import Radio, delta, select_active
from awqm.analysis import State, advance, radio_score, retry_ratio, client_quality, client_impact
from awqm.collector import command, TaskQueue, Task
from awqm.parser import normalized_snapshot
from awqm.demo import seed
from awqm.storage import Store

def radio(t=60, u=90, i=5, n=-94, **kw):
    return replace(Radio("ap:0","ap","AP","w1","5GHz",44,t,u,i,n),**kw)

class AnalysisTests(unittest.TestCase):
    def test_radio_scores(self):
        self.assertEqual(radio_score(radio()),50)
        self.assertEqual(radio_score(radio(u=10)),100)
        self.assertIsNone(radio_score(radio(u=None)))
    def test_hysteresis(self):
        s=State()
        for t in (60,120,180):
            s=advance(s,radio(t))
        self.assertEqual(s.status,"PERSISTENT")
        s=advance(s,radio(240,u=70))
        self.assertTrue(s.open_incident)
        for t in (300,360,420):
            s=advance(s,radio(t,u=40))
        self.assertEqual(s.status,"RESOLVED")
        self.assertFalse(s.open_incident)
    def test_missing_never_recovers(self):
        s=State()
        for t in (60,120,180):
            s=advance(s,radio(t))
        for t in (240,300,360):
            s=advance(s,radio(t,u=None))
        self.assertEqual(s.status,"UNKNOWN")
        self.assertTrue(s.open_incident)
    def test_gap_duplicate_epoch(self):
        s=advance(State(),radio())
        self.assertEqual(advance(s,radio()),s)
        s=advance(s,radio(180))
        self.assertEqual(s.bad,1)
        s=advance(s,radio(240,epoch="new"))
        self.assertEqual(s.bad,1)
    def test_retry_and_counter(self):
        self.assertIsNone(delta(100,3,"a","a"))
        self.assertIsNone(delta(100,200,"a","b"))
        self.assertEqual(delta(100,200,"a","a"),100)
        self.assertEqual(retry_ratio(100,35),35)
        self.assertIsNone(retry_ratio(10,2))
    def test_client(self):
        self.assertEqual(client_quality(90,35,0),(100,"complete"))
        self.assertEqual(client_quality(90,35,None),(100,"partial"))
        self.assertIsNone(client_impact(40,"measured","whole_interval",False,3,2,True))
        a=client_impact(40,"measured","whole_interval",True,3,2,True)
        self.assertEqual(a["score"],100)
        self.assertFalse(a["verified"])
        self.assertFalse(client_impact(40,"measured","whole_interval",True,3,0,True)["candidate"])
    def test_anchor(self):
        r=radio()
        self.assertEqual(select_active([("A",r),("SA",replace(r,controller="w2"))])[0],r)
        self.assertEqual(select_active([("A",r),("A",replace(r,controller="w2"))])[1],"Conflict")

class CollectorTests(unittest.TestCase):
    def test_allowlist(self):
        self.assertEqual(command("show ap radio-summary"),"show ap radio-summary")
        with self.assertRaises(ValueError):command("reload")
        with self.assertRaises(ValueError):command("show ap arm neighbors","AP\nreload")
        self.assertEqual(command("show ap arm neighbors","AP-1"),"show ap arm neighbors ap-name AP-1")
    def test_queue(self):
        q=TaskQueue(2)
        a,b=Task("a","x",4),Task("b","y",1)
        self.assertEqual(q.submit(a),"queued")
        self.assertEqual(q.submit(a),"duplicate")
        q.submit(b)
        self.assertEqual(q.submit(Task("c","z",3)),"full")
        self.assertEqual(q.next(),b)
        self.assertIsNone(q.next())
        self.assertEqual(q.submit(b),"duplicate")
        q.finish("b")
        self.assertEqual(q.next(),a)

class DataTests(unittest.TestCase):
    def test_parser(self):
        f={"schema":"awqm.synthetic.radio.v1","count":1,"complete":True,"rows":[asdict(radio())]}
        self.assertEqual(len(normalized_snapshot(json.dumps(f))),1)
        f["complete"]=False
        with self.assertRaises(ValueError): normalized_snapshot(json.dumps(f))
        with self.assertRaises(ValueError): normalized_snapshot("{}")
    def test_storage(self):
        with tempfile.TemporaryDirectory() as root:
            s=Store(root)
            self.assertTrue(s.save(radio()))
            self.assertFalse(s.save(radio()))
            with self.assertRaises(ValueError):s.save(radio(u=20))
            self.assertTrue(s.save(radio(120)))
            self.assertFalse(s.save(radio(1)))
            self.assertEqual(s.snapshot()["radios"][0]["observed_at"],120)
    def test_demo(self):
        with tempfile.TemporaryDirectory() as root:
            s=Store(root)
            seed(s,180)
            snap=s.snapshot()
            self.assertEqual(len(snap["radios"]),12)
            self.assertEqual(sum(r["open_incident"] for r in snap["radios"]),3)
            self.assertTrue(all(r["source"]=="synthetic" for r in snap["radios"]))
if __name__=="__main__":
    unittest.main()
