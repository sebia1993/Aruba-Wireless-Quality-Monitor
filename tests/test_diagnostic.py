import tempfile,unittest
from pathlib import Path
from awqm.diagnostic import collect_controller,run_trial
from awqm.storage import Store
from test_aos8 import sample
from test_client import table

class Clock:
    t=120
    def now(self):return self.t

class Session:
    def __init__(self,mode="normal"):
        inv,active,radio,assoc=sample()
        self.responses={"show cpuload":"user 10%, system 10%, idle 80%",
            "show ap radio-summary":radio,"show ap active details":active,
            "show ap database long":inv,"show ap association":assoc,
            "show ap bss-table":"% Invalid command"}
        if mode=="high":self.responses["show cpuload"]="user 50%, system 30%, idle 20%"
        self.mode=mode;self.closed=False;self.calls=[]
    def open(self):
        if self.mode=="auth":
            class AuthenticationException(Exception):pass
            raise AuthenticationException()
    def run(self,name,**kwargs):
        self.calls.append(name)
        return self.responses[name]
    def close(self):self.closed=True

class DiagnosticTests(unittest.TestCase):
    def test_limited_round_and_partial_failure(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(root,mode="diagnostic");clock=Clock()
            a,b=Session(),Session("high")
            result=run_trial([("W1","a"),("W2","b")],lambda h:a if h=="a" else b,store,clock.now,clock.now)
            self.assertEqual(result["coherent_controllers"],1)
            self.assertEqual(result["merged_radios"],2)
            self.assertEqual(result["written"],2)
            self.assertEqual(b.calls,["show cpuload"])
            self.assertTrue(a.closed and b.closed)
            self.assertEqual(store.snapshot()["mode"],"diagnostic")
            self.assertFalse(result["field_validated"])
            self.assertTrue(all(r["status"]=="UNKNOWN" and r["score"] is None for r in store.snapshot()["radios"]))
            self.assertEqual(store.snapshot()["events"],[])
            self.assertTrue(all(r["source"]=="aos8-cli-unvalidated" for r in store.snapshot()["radios"]))
    def test_authentication_stop(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(root,mode="diagnostic");clock=Clock()
            calls=[];s=Session("auth")
            def factory(host):
                calls.append(host);return s
            r=run_trial([("W1","a"),("W2","b")],factory,store,clock.now,clock.now)
            self.assertEqual(calls,["a"])
            self.assertEqual(r["status"],"authentication_rejected")
            self.assertEqual(r["connections"]["W2"],"authentication_attempts_stopped")
            self.assertTrue(s.closed)
    def test_budget_deferral(self):
        c=Clock();s=Session()
        r=collect_controller("W1",s,c.now,c.now,budget=0)
        self.assertIsNone(r.snapshot)
        self.assertEqual(s.calls,[])
        self.assertTrue(all(x["status"]=="budget_deferred" for x in r.commands))
    def test_no_synthetic_data_in_diagnostic(self):
        from awqm.demo import synthetic_rows
        with tempfile.TemporaryDirectory() as root:
            s=Store(root,mode="diagnostic")
            with self.assertRaises(ValueError):s.save(next(synthetic_rows(120)))
