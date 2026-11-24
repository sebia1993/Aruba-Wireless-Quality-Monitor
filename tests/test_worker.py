import unittest
from awqm.worker import ControllerWorker, ClusterWorkers
from test_aos8 import sample

class Clock:
    def __init__(self):self.t=0
    def now(self):return self.t

class Session:
    def __init__(self,responses):
        self.responses=responses;self.calls=[];self.closed=False
    def open(self):pass
    def run(self,name,timeout=30):
        self.calls.append(name)
        result=self.responses[name]
        if isinstance(result,Exception):raise result
        return result
    def close(self):self.closed=True

def setup():
    inv,active,radio,assoc=sample()
    clock=Clock()
    responses={"show cpuload":"user 10%, system 10%, idle 80%",
               "show ap radio-summary":radio,"show ap database long":inv,
               "show ap active details":active,"show ap association":assoc}
    session=Session(responses)
    w=ControllerWorker("W1",lambda:session,clock.now,clock.now,mode="standard")
    return clock,session,w

class WorkerTests(unittest.TestCase):
    def test_complete_pipeline(self):
        clock,session,w=setup()
        for _ in range(5):
            self.assertEqual(w.tick().status,"success")
        self.assertIsNone(w.tick())
        data=ClusterWorkers([w]).coherent()
        self.assertEqual(data["coverage"],1)
        self.assertEqual(len(data["radios"]),2)
        self.assertEqual(len(data["clients"]),1)
        clock.t=46
        self.assertIsNone(w.snapshot())
    def test_partial_failure_is_not_empty(self):
        clock,session,w=setup()
        for _ in range(5):w.tick()
        clock.t=61
        session.responses["show ap radio-summary"]="Total Radios: 0"
        w.tick();r=w.tick()
        self.assertEqual(r.status,"parse_error")
        self.assertIn("show ap radio-summary",w.latest)
        self.assertIsNone(w.snapshot())
    def test_unsupported_disables(self):
        clock,session,w=setup()
        session.responses["show ap radio-summary"]="% Invalid command"
        w.tick()
        self.assertEqual(w.tick().status,"unsupported")
        self.assertIn("show ap radio-summary",w.disabled)
        self.assertEqual(w.schedule.due["show ap radio-summary"],float("inf"))
    def test_cpu_denied_invalidates_protection(self):
        clock,session,w=setup()
        self.assertEqual(w.tick().status,"success")
        self.assertEqual(w.schedule.protection.level,"normal")
        clock.t=31
        session.responses["show cpuload"]="Permission denied"
        self.assertEqual(w.tick().status,"permission_denied")
        self.assertEqual(w.schedule.protection.level,"unknown")
    def test_transport_backoff(self):
        clock,session,w=setup()
        session.responses["show cpuload"]=TimeoutError()
        self.assertEqual(w.tick().status,"transport_error")
        self.assertTrue(session.closed)
        self.assertEqual(w.next_connect,15)
        self.assertIsNone(w.tick())
    def test_partial_cluster_coverage(self):
        c,s,w=setup()
        for _ in range(5):w.tick()
        other=ControllerWorker("W2",lambda:Session({}),c.now,c.now)
        result=ClusterWorkers([w,other]).coherent()
        self.assertEqual((result["coverage"],result["expected"]),(1,2))
