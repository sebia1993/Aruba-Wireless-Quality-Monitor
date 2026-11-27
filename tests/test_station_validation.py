import unittest,tempfile,json
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace
from awqm.station_validation import (age_seconds,stamp,continuous,collect,successful_retry,
    StationApproval,load_station_approvals,CHECKS,StationUnavailable,AssociationStamp)
from awqm.aos8 import ParseError
from awqm.live_clients import ClientApproval,LiveClients
from awqm.worker import Reading
from awqm.airtime_contract import Counter,measured
from awqm.jobs import Jobs
from awqm.collector import command
from test_aos8 import table
from test_live_clients import fixture,MAC,BSS,SSID

def association(age="7m:51s",count=1,aid=2):
    fields=["Name","bssid","mac","auth","assoc","aid","essid","assoc. time","num assoc","Flags"]
    return table("Association Table",fields,[["DEMO-AP",BSS,MAC,"y","y",aid,SSID,age,count,"WAB"]],"Num Clients:1")
def stats(sent,retried):
    return f"Station Stats\nParameter Value\nFrames Transmitted {sent}\nSuccess With Retry {retried}"

class StationTests(unittest.TestCase):
    def test_age_and_continuity(self):
        self.assertEqual(age_seconds("7m:51s"),471)
        self.assertEqual(age_seconds("1d:2h:3m:4s"),93784)
        self.assertEqual(age_seconds("15s"),15)
        self.assertIsNone(age_seconds("7m:99s"))
        self.assertIsNone(age_seconds("00:01:10"))
        a=stamp(association(),MAC,1000)
        self.assertTrue(continuous(a,stamp(association("7m:52s"),MAC,1001),10))
        self.assertFalse(continuous(a,stamp(association("1s"),MAC,1001),10))
        self.assertFalse(continuous(a,stamp(association("7m:52s",2),MAC,1001),10))
    def test_bracketed_query(self):
        class Session:
            def __init__(self,responses):self.responses=list(responses);self.calls=[]
            def run(self,name,target,**kw):self.calls.append(name);return self.responses.pop(0)
        s=Session([association(),stats(1000,100),association()])
        r=collect(s,MAC,lambda:0,lambda:1000,10)
        self.assertEqual(s.calls,["show ap association client-mac","show ap debug client-stats","show ap association client-mac"])
        self.assertEqual(r["counters"]["sent"],1000)
        self.assertEqual(command("show ap association client-mac",MAC),"show ap association client-mac "+MAC)
        with self.assertRaises(ValueError):command("show ap association client-mac",MAC+"\nshow version")
        with self.assertRaises(ParseError):collect(Session([association(),stats(1000,100),association("2s")]),MAC,lambda:0,lambda:1000,10)
        with self.assertRaises(StationUnavailable):collect(Session(["Permission denied"]),MAC,lambda:0,lambda:1000,10)
    def test_retry_is_successful_subset(self):
        a={"sent":1000,"retried":100};b={"sent":1200,"retried":170}
        self.assertEqual(successful_retry(a,b,"a","a"),35)
        self.assertIsNone(successful_retry(a,{"sent":1050,"retried":120},"a","a"))
        self.assertIsNone(successful_retry(a,b,"a","b"))
        self.assertIsNone(successful_retry(a,{"sent":900,"retried":150},"a","a"))
    def test_station_attestation(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root);(p/"config").mkdir()
            cfg=SimpleNamespace(root=p,site="fixture")
            a=ClientApproval("radio",900,2000,True,"client")
            d={"controller":"w1","client_tag":"client","approved_at":950,"valid_until":1800,
               "reviewed_by":"fixture","checks":{k:True for k in CHECKS}}
            doc={"version":1,"enabled":True,"site":"fixture","contract":"aos8-station-success-retry-v1","controllers":[d]}
            f=p/"config/station-validation.json";f.write_text(json.dumps(doc))
            self.assertTrue(load_station_approvals(cfg,{"w1":a},1000)["w1"].permits("client",1000,1000))
            d["checks"]["counter_scope"]=False;f.write_text(json.dumps(doc))
            with self.assertRaises(ValueError):load_station_approvals(cfg,{"w1":a},1000)
    def test_live_interval_and_reset(self):
        r,s,q,a,assoc,bss,d=fixture()
        l=LiveClients({"w1":ClientApproval("tag",900,1900,True,"ct")},key=b"fixture",
                      station_approvals={"w1":StationApproval("ct",900,1800)})
        l.ingest(d,"w1",s,assoc,bss,[r],q,{MAC:a},1010)
        def read(t,age,sent,retried,count=1):
            x=AssociationStamp(a["ap_name"],BSS,SSID,2,count,age,t)
            return Reading("show ap debug client-stats","success",t,{"before":x,"after":x,"counters":{"sent":sent,"retried":retried}},generation=1,target=MAC)
        self.assertEqual(l.ingest(read(1010,471,1000,100),"w1",s,assoc,bss,[r],q,{MAC:a},1010),1)
        self.assertIsNone(l.publish([r],[s],q,{MAC:a},1010)[0]["station_retry"])
        self.assertEqual(l.ingest(read(1055,516,1200,170),"w1",s,assoc,bss,[r],q,{MAC:a},1055),1)
        v=l.publish([r],[s],q,{MAC:a},1055)[0]
        self.assertEqual(v["station_retry"],35)
        self.assertEqual(v["station_tx_activity"],"successful_tx_observed")
        failed=Reading("show ap debug client-stats","parse_error",1056,generation=1,target=MAC)
        self.assertEqual(l.ingest(failed,"w1",s,assoc,bss,[r],q,{MAC:a},1056),0)
        self.assertIsNone(l.publish([r],[s],q,{MAC:a},1056)[0]["station_retry"])
        self.assertIsNone(v["retry"]);self.assertIsNone(v["airtime"]);self.assertIsNone(v["candidate"])
        self.assertEqual(l.ingest(read(1085,2,1300,200),"w1",s,assoc,bss,[r],q,{MAC:a},1085),1)
        self.assertIsNone(l.publish([r],[s],q,{MAC:a},1085)[0]["station_retry"])
        for secret in (MAC,BSS,SSID):self.assertNotIn(secret,json.dumps(l.station))
    def test_counter_epoch_restarts_on_changed_approval_and_hides_old_ratio(self):
        r,s,q,a,assoc,bss,d=fixture()
        original=StationApproval("ct",900,1800,"station-v1")
        l=LiveClients({"w1":ClientApproval("tag",900,1900,True,"ct")},key=b"fixture",
                      station_approvals={"w1":original})
        self.assertEqual(l.ingest(d,"w1",s,assoc,bss,[r],q,{MAC:a},1010),1)
        ident=l.token("client",MAC)
        def read(t,age,sent,retried):
            x=AssociationStamp(a["ap_name"],BSS,SSID,2,1,age,t)
            return Reading("show ap debug client-stats","success",t,
                {"before":x,"after":x,"counters":{"sent":sent,"retried":retried}},
                generation=1,target=MAC)
        def accept(t,age,sent,retried):
            self.assertEqual(l.ingest(read(t,age,sent,retried),"w1",s,assoc,bss,[r],q,{MAC:a},t),1)
        accept(1010,471,1000,100)
        accept(1040,501,1200,170)
        old_sequence=l.station[ident]["sequence"]
        self.assertEqual(l.publish([r],[s],q,{MAC:a},1040)[0]["station_retry"],35)
        l.station_approvals["w1"]=replace(original,tag="station-v2",approved_at=1041)
        # Never relabel the old counter interval under a replacement approval.
        view=l.publish([r],[s],q,{MAC:a},1041)[0]
        self.assertIsNone(view["station_retry"])
        self.assertIsNone(view["station_start"])
        self.assertNotIn(ident,l.station)
        accept(1070,531,1400,240)
        self.assertNotEqual(l.station[ident]["sequence"],old_sequence)
        self.assertIsNone(l.publish([r],[s],q,{MAC:a},1070)[0]["station_retry"])
        # Refresh normal metadata and show that a new same-approval interval
        # can produce the successful-retry ratio, not an Airtime estimate.
        r=replace(r,observed_at=1100)
        s=replace(s,observed_at=1100)
        q={"r1":{"observed_at":1100,"status":"PERSISTENT","score":50}}
        assoc=replace(assoc,observed_at=1099)
        bss=replace(bss,observed_at=1098)
        self.assertEqual(l.ingest(replace(d,observed_at=1100),"w1",s,assoc,bss,[r],q,{MAC:a},1100),1)
        accept(1100,561,1600,310)
        view=l.publish([r],[s],q,{MAC:a},1100)[0]
        self.assertEqual(view["station_retry"],35)
        self.assertIsNone(view["airtime"])
        self.assertIsNone(view["retry"])
        for secret in ("station-v1","station-v2",MAC,BSS,SSID):
            self.assertNotIn(secret,json.dumps(view))
        l.station_approvals["w1"]=replace(l.station_approvals["w1"],valid_until=1100)
        self.assertIsNone(l.publish([r],[s],q,{MAC:a},1100)[0]["station_retry"])
        self.assertNotIn(ident,l.station)

    def test_counter_epoch_checks_start_and_end_of_each_bracket(self):
        r,s,q,a,assoc,bss,d=fixture()
        l=LiveClients({"w1":ClientApproval("tag",900,1900,True,"ct")},key=b"fixture",
                      station_approvals={"w1":StationApproval("ct",1005,1800,"station")})
        self.assertEqual(l.ingest(d,"w1",s,assoc,bss,[r],q,{MAC:a},1010),1)
        ident=l.token("client",MAC)
        def read(t,begin,begin_age,end,end_age,sent,retried):
            before=AssociationStamp(a["ap_name"],BSS,SSID,2,1,begin_age,begin)
            after=replace(before,at=end,age=end_age)
            return Reading("show ap debug client-stats","success",t,
                {"before":before,"after":after,"counters":{"sent":sent,"retried":retried}},
                generation=1,target=MAC)
        def ingest(reading):
            return l.ingest(reading,"w1",s,assoc,bss,[r],q,{MAC:a},reading.observed_at)
        # Approval must cover the complete bracket, not just receipt time.
        self.assertEqual(ingest(read(1010,1004,465,1010,471,1000,100)),0)
        self.assertNotIn(ident,l.station)
        self.assertEqual(ingest(read(1010,1010,471,1010,471,1000,100)),1)
        first=l.station[ident]["sequence"]
        # End-to-end age appears plausible, and within-bracket tolerance
        # passes, but prior end -> new start is inconsistent by four seconds.
        self.assertEqual(ingest(read(1055,1050,507,1055,515,1200,170)),1)
        self.assertNotEqual(l.station[ident]["sequence"],first)
        self.assertIsNone(l.publish([r],[s],q,{MAC:a},1055)[0]["station_retry"])
        # A bracket cannot finish after its receipt timestamp.
        self.assertEqual(ingest(read(1060,1059,519,1061,521,1300,200)),0)
        self.assertNotIn(ident,l.station)

    def test_terminal_station_targets_removed(self):
        q=Jobs();j=q.submit("r","show ap debug client-stats",MAC,100)
        q.start(j.job_id,100);q.finish(j.job_id,"completed","done",101)
        q.purge_station(101)
        self.assertIsNone(q.get(j.job_id));self.assertNotIn(MAC,repr(q.records))
    def test_airtime_contract(self):
        a=Counter(1000,1_000_000,"us","whole_interval","epoch","metric",True)
        b=replace(a,at=1060,value=31_000_000)
        self.assertEqual(measured(a,b).value,50)
        for bad in (replace(b,validated=False),replace(b,unit="ms"),replace(b,epoch="new"),
                    replace(b,value=0),replace(b,value=100_000_000),replace(b,scope="tx")):
            self.assertIsNone(measured(a,bad))
        self.assertEqual(measured(replace(a,scope="tx"),replace(b,scope="tx")).scope,"tx")

    def test_worker_live_station_job(self):
        from awqm.worker import ControllerWorker
        from awqm.fixture_session import FixtureSession
        from awqm.field_validation import Approval
        from unittest.mock import patch
        from awqm.aos8 import associations
        sess=FixtureSession(1);now=[1000]
        sess.responses["show version"]="ArubaOS (MODEL: Aruba7240XM), Version 8.10.0.24"
        original=sess.run
        target=associations(sess.responses["show ap association"])[0]["client_mac"]
        row=associations(sess.responses["show ap association"])[0]
        calls=[]
        def run(name,target=None,**kwargs):
            if name=="show ap association client-mac":
                calls.append(name)
                return table("Association Table",["Name","bssid","mac","auth","assoc","aid","essid","assoc. time","num assoc","Flags"],
                    [[row["ap_name"],row["bssid"],row["client_mac"],"y","y",2,row["ssid"],"7m:51s",1,"WAB"]],"Num Clients:1")
            if name=="show ap debug client-stats":calls.append(name)
            return original(name,target,**kwargs)
        sess.run=run
        a=Approval("w1","192.0.2.1","Aruba7240XM","8.10.0.24","fixture-key",900,2000,"tag")
        w=ControllerWorker("w1",lambda:sess,lambda:now[0],lambda:now[0],mode="standard",
            enable_bss=True,field_approval=a,live_detail=True,client_approval=ClientApproval("tag",900,1900,True,"ct"),
            station_approval=StationApproval("ct",900,1800))
        with patch("awqm.worker.fingerprint",return_value="fixture-key"):
            for _ in range(7):self.assertEqual(w.tick().status,"success")
            j=w.submit_detail("station-fixture","show ap debug client-stats",target)
            self.assertTrue(w._live_detail_allowed(j))
            reading=w.tick()
            self.assertEqual(reading.status,"success")
            self.assertEqual(calls,["show ap association client-mac","show ap debug client-stats","show ap association client-mac"])
            self.assertIsNone(w.jobs.get(j.job_id))
            self.assertFalse(w.detail_latest)
        w.close()
