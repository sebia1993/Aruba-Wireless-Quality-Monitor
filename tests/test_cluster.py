import unittest,tempfile,time,json
from pathlib import Path
from dataclasses import replace
from awqm.cluster import group_membership,ap_load,client_load,ClusterMonitor,MEMBERSHIP,AP_LOAD,CLIENT_LOAD
from awqm.aos8 import ParseError
from awqm.worker import Reading,ControllerWorker
from awqm.engine import Engine
from awqm.storage import Store
from awqm.fixture_session import FixtureSession

IPS={"w1":"192.0.2.1","w2":"192.0.2.2"}

def membership(source="w1",peer_state="CONNECTED",omit_peer=False):
    other="w2" if source=="w1" else "w1"
    return ("Cluster Enabled, Profile Name = \"fixture\"\nVRRP Passphrase: SHOULD-NOT-LEAVE-PARSER\n"
            "Cluster Info Table\n------------------\nType IPv4 Address Priority Connection-Type STATUS\n"
            "---- --------------- -------- --------------- ------\n"
            f"self {IPS[source]} 128 N/A CONNECTED (Member)\n"+
            ("" if omit_peer else f"peer {IPS[other]} 128 L2-Connected {peer_state} (Leader, last HBT_RSP 11ms ago, RTD = 0.2 ms)\n"))

def load(source="w1",kind="APs",counts=(15,25),bad=False):
    rows=[f"{'self' if k==source else 'peer'} {IPS[k]} {counts[i]} {counts[1-i]}" for i,k in enumerate(IPS)]
    total=sum(counts)
    return (f"Cluster Load Distribution for {kind}\n------------------\nType IPv4 Address Active {kind} Standby {kind}\n"
            "---- --------------- ------ ------\n"+"\n".join(rows)+f"\nTotal: Active {kind} {total+int(bad)} Standby {kind} {total}")

def frame(source,at=1000,peer_state="CONNECTED",counts=(15,25),disabled=(),omit_peer=False):
    return {"connected":True,"disabled":disabled,"readings":{
        MEMBERSHIP:Reading(MEMBERSHIP,"success",at,group_membership(membership(source,peer_state,omit_peer))),
        "show cpuload":Reading("show cpuload","success",at,20),
        AP_LOAD:Reading(AP_LOAD,"success",at,ap_load(load(source,counts=counts))),
        CLIENT_LOAD:Reading(CLIENT_LOAD,"success",at,client_load(load(source,"Clients",counts=(100,200))))
    }}

class ClusterParserTests(unittest.TestCase):
    def test_parser_and_no_preamble_secrets(self):
        rows=group_membership(membership())
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[1]["role"],"leader")
        self.assertEqual(rows[1]["link"],"L2")
        self.assertNotIn("SHOULD-NOT",json.dumps(rows))
        self.assertEqual(sum(x["active"] for x in ap_load(load())),40)
        self.assertEqual(len(client_load(load(kind="Clients"))),2)
        for bad in (load(bad=True),load().split("Total:")[0],load()+"\n--More--"):
            with self.assertRaises(ParseError):ap_load(bad)
        with self.assertRaises(ParseError):group_membership(membership()+"\nwrapped")
        self.assertEqual(group_membership(membership(peer_state="INCOMPATIBLE"))[1]["state"],"INCOMPATIBLE")
    def test_no_double_count_or_false_down(self):
        m=ClusterMonitor(IPS);frames={k:frame(k) for k in IPS}
        r=m.report(frames,1000)
        self.assertEqual(r["load"]["ap"]["status"],"crosschecked")
        self.assertEqual(r["load"]["ap"]["active_total"],40)
        self.assertEqual(r["load"]["client"]["active_total"],300)
        self.assertNotIn("192.0.2.",json.dumps(r))
        frames["w2"]=frame("w2",counts=(20,25))
        self.assertIsNone(m.report(frames,1000)["load"]["ap"]["active_total"])
        self.assertEqual(m.report(frames,1000)["load"]["ap"]["status"],"conflicting_observations")
        frames={"w1":frame("w1",peer_state="DISCONNECTED")}
        r=m.report(frames,1000)
        self.assertEqual(r["nodes"][1]["state"],"peer_issue_observed")
        self.assertIsNone(r["nodes"][1]["cpu"])
        frames={"w1":frame("w1",omit_peer=True)}
        self.assertEqual(m.report(frames,1000)["nodes"][1]["state"],"unknown")
        self.assertIsNone(m.report(frames,1000)["load"]["ap"]["active_total"])
        self.assertIsNone(m.report(frames,1200)["load"]["client"]["active_total"])
    def test_representative_handover(self):
        m=ClusterMonitor(IPS);frames={k:frame(k) for k in IPS}
        self.assertEqual(m.choose(frames,1000),"w1")
        frames={"w1":frame("w1",1031,disabled=(AP_LOAD,)),"w2":frame("w2",1031)}
        self.assertEqual(m.choose(frames,1031,("w1",)),"w1")
        self.assertEqual(m.choose(frames,1031),"w2")
    def test_worker_poll_gate(self):
        s=FixtureSession(1);t=[1000]
        s.responses.update({MEMBERSHIP:membership(),AP_LOAD:load(),CLIENT_LOAD:load(kind="Clients")})
        calls=[];original=s.run
        def run(name,*a,**kw):
            calls.append(name);return original(name,*a,**kw)
        s.run=run
        w=ControllerWorker("w1",lambda:s,lambda:t[0],lambda:t[0],mode="standard",enable_cluster=True)
        for _ in range(10):w.tick(cluster_load=False)
        self.assertIn(MEMBERSHIP,calls);self.assertNotIn(AP_LOAD,calls);self.assertNotIn(CLIENT_LOAD,calls)
        for _ in range(5):w.tick(cluster_load=True)
        self.assertIn(AP_LOAD,calls);self.assertIn(CLIENT_LOAD,calls)
        self.assertIn(AP_LOAD,w.cluster_frame()["readings"])
        w.close()
    def test_coordinator_load_is_single_representative(self):
        now=[1000]
        def clock():return now[0]
        calls={k:[] for k in IPS}
        workers=[]
        for idx,name in enumerate(IPS,1):
            s=FixtureSession(idx)
            s.responses.update({MEMBERSHIP:membership(name),AP_LOAD:load(name),CLIENT_LOAD:load(name,"Clients")})
            orig=s.run
            def run(cmd,*a,source=name,original=orig,**kw):
                calls[source].append(cmd);return original(cmd,*a,**kw)
            s.run=run
            workers.append(ControllerWorker(name,lambda s=s:s,clock,clock,mode="standard",enable_cluster=True))
        with tempfile.TemporaryDirectory() as root:
            e=Engine(workers,Store(root,mode="diagnostic"),clock=clock,wall=clock,cluster_monitor=ClusterMonitor(IPS))
            try:
                for _ in range(60):
                    e.pulse();time.sleep(.02);now[0]+=1
                    if e.cluster_view and e.cluster_view["load"]["client"]["active_total"]==300 and e.cluster_view["membership_sources"]==2:break
                self.assertEqual(e.cluster_view["load"]["ap"]["active_total"],40)
                self.assertEqual(e.cluster_view["load"]["client"]["active_total"],300)
                self.assertIn(MEMBERSHIP,calls["w2"])
                self.assertNotIn(AP_LOAD,calls["w2"])
                self.assertNotIn(CLIENT_LOAD,calls["w2"])
            finally:e.close()
