import json,tempfile,unittest
from dataclasses import replace
from awqm.aos8 import ControllerSnapshot
from awqm.controller_load_review import ControllerLoadHistory,review_controller_load,CPU
from awqm.controller_resources import DATAPATH
from awqm.diagnostic_view import DiagnosticView
from awqm.model import Radio
from awqm.storage import Store
from awqm.worker import Reading

class ControllerLoadReviewTests(unittest.TestCase):
    def setUp(self):
        self.now=1000;self.tag="fixture-approval-tag"
        self.snapshot=ControllerSnapshot("w1",1000,[],[],[],field_tag=self.tag,field_until=1100,field_connection="fixture:3")
        self.frame={"connected":True,"_field_connection":"fixture:3","_resource_radio_tag":self.tag,
            "resource_validation":{"valid":True,"approved_at":900,"until":1100,"cpu_ids":(0,)},
            "readings":{CPU:Reading(CPU,"success",970,70,generation=3),
                DATAPATH:Reading(DATAPATH,"success",1000,{"rows":[{"id":0,"type":"FP","percent_1s":25,"percent_4s":20}]},generation=3)}}
        self.radios=[self.radio(1,0),self.radio(1,1),self.radio(2,0)]
        self.quality={r.radio_id:{"scope":"local_operator_attested_radio","status":"PERSISTENT","incident_id":"private",
            "observed_at":1000,"approval":self.tag[:12],"valid_until":1100} for r in self.radios}
    def radio(self,ap,physical):
        key=f"site:00:11:22:33:44:{ap:02x}"
        return Radio(key+":"+str(physical),key,"private-ap","w1","5GHz",44,1000,90,30,-90,
            source="aos8-cli-unvalidated")
    def review(self,radios=None):
        return review_controller_load("w1",self.frame,self.snapshot,self.radios if radios is None else radios,self.quality,self.now)
    def test_distinct_physical_aps_and_exact_window(self):
        r=self.review()
        self.assertEqual((r["code"],r["confidence"],r["ap_count"],r["radio_count"]),("CL01","low",2,3))
        self.assertEqual((r["cpu_percent"],r["datapath_max_4s_percent"],r["baseline"]),(70,20,"unavailable"))
        self.assertFalse(r["verified"]);self.assertEqual(r["service_impact"],"unverified")
        self.assertNotIn("private",json.dumps(r));self.assertNotIn("00:11",json.dumps(r))
        self.assertEqual(self.review(self.radios[:2])["reason"],"distinct_persistent_aps_required")
        self.frame["readings"][CPU]=replace(self.frame["readings"][CPU],observed_at=969)
        self.assertEqual(self.review()["reason"],"time_unaligned")
        self.frame["readings"][CPU]=replace(self.frame["readings"][CPU],observed_at=970,data=69)
        self.assertEqual(self.review()["reason"],"cpu_screen_not_met")
    def test_attestation_generation_and_current_quality_are_required(self):
        self.frame["_field_connection"]="fixture:4"
        self.assertEqual(self.review()["reason"],"matching_attestation_required")
        self.frame["_field_connection"]="fixture:3"
        self.frame["_resource_radio_tag"]="different"
        self.assertEqual(self.review()["reason"],"matching_attestation_required")
        self.frame["_resource_radio_tag"]=self.tag
        old=self.frame["readings"][DATAPATH]
        self.frame["readings"][DATAPATH]=replace(old,generation=4)
        self.assertEqual(self.review()["reason"],"resource_evidence_required")
        self.frame["readings"][DATAPATH]=old
        q=self.quality[self.radios[-1].radio_id]
        for key,value in (("observed_at",999),("valid_until",1000),("status","RECOVERING"),("approval","different"),("incident_id",None)):
            with self.subTest(key=key):
                self.quality[self.radios[-1].radio_id]=dict(q,**{key:value})
                self.assertEqual(self.review()["reason"],"distinct_persistent_aps_required")
        self.quality[self.radios[-1].radio_id]=q
        self.assertEqual(self.review(self.radios[:2]+[replace(self.radios[-1],utilization=10,interference=0)])["reason"],"distinct_persistent_aps_required")
        self.frame["resource_validation"]["cpu_ids"]=(1,)
        self.assertEqual(self.review()["reason"],"resource_evidence_required")
    def test_current_window_contrary_observations_are_limited_and_attested(self):
        def normal(ap,physical):
            r=replace(self.radio(ap,physical),utilization=20,interference=2,noise=-95)
            self.quality[r.radio_id]={"scope":"local_operator_attested_radio","status":"NORMAL",
                "incident_id":None,"observed_at":1000,"approval":self.tag[:12],"valid_until":1005}
            return r
        other=[normal(3,0),normal(3,1),normal(4,0)]
        same=normal(1,2)
        self.assertEqual(self.review()["counter_evidence"],{"status":"not_observed","other_ap_count":0,"normal_radio_count":0})
        self.assertEqual(self.review(self.radios+[same])["counter_evidence"]["status"],"not_observed")
        result=self.review(self.radios+other+[same])
        self.assertEqual(result["counter_evidence"],{"status":"observed","other_ap_count":2,"normal_radio_count":3})
        self.assertEqual((result["confidence"],result["valid_until"]),("low",1005))
        self.assertFalse(result["verified"])
        self.assertNotIn("00:11",json.dumps(result))
        r=other[-1];q=self.quality[r.radio_id]
        for key,value in (("approval","other"),("observed_at",999),("valid_until",1000),
                          ("status","UNKNOWN"),("incident_id","open"),("scope","other")):
            with self.subTest(key=key):
                self.quality[r.radio_id]=dict(q,**{key:value})
                self.assertEqual(self.review(self.radios+other)["counter_evidence"]["other_ap_count"],1)
        self.quality[r.radio_id]=q
        self.assertEqual(self.review(self.radios+other[:2]+[replace(r,utilization=50)])["counter_evidence"]["other_ap_count"],1)
        self.assertEqual(self.review(self.radios+other[:2]+[replace(r,controller="w2")])["counter_evidence"]["other_ap_count"],1)
        with tempfile.TemporaryDirectory() as root:
            clock=[1000]
            view=DiagnosticView(Store(root,mode="diagnostic"),lambda:clock[0])
            view.publish({"cluster":{"nodes":[{"controller":"w1","load_review":result}]}},[])
            self.assertEqual(view.page()["cluster"]["nodes"][0]["load_review"]["counter_evidence"],result["counter_evidence"])
            clock[0]=1005
            self.assertEqual(view.page()["cluster"]["nodes"][0]["load_review"]["status"],"not_evaluated")
    def test_pre_risk_resource_pair_exact_window_and_scope(self):
        history=ControllerLoadHistory()
        self.frame["resource_validation"].update(approved_at=100,until=3000)
        def at(t,cpu=70,skew=30):
            self.now=t
            self.snapshot=replace(self.snapshot,observed_at=t,field_until=3000)
            self.frame["readings"][CPU]=replace(self.frame["readings"][CPU],observed_at=t-skew,data=cpu)
            self.frame["readings"][DATAPATH]=replace(self.frame["readings"][DATAPATH],observed_at=t)
            radios=[replace(r,observed_at=t) for r in self.radios]
            quality={r.radio_id:dict(self.quality[r.radio_id],observed_at=t,valid_until=3000,
                        incident_first_at=1600 if i<2 else 1660) for i,r in enumerate(radios)}
            return review_controller_load("w1",self.frame,self.snapshot,radios,quality,t,history)
        # Earlier eligible pair, exact upper boundary and pair outside it.
        self.assertEqual(at(730,20)["reason"],"cpu_screen_not_met")
        self.assertEqual(at(1300,42)["reason"],"cpu_screen_not_met")
        self.assertEqual(at(1301,10)["reason"],"cpu_screen_not_met")
        result=at(2000)
        self.assertEqual(result["baseline"],"observed")
        self.assertEqual(result["baseline_observation"],{"cpu_at":1270,"cpu_percent":42,
            "datapath_at":1300,"datapath_max_1s_percent":25,"datapath_max_4s_percent":20,"risk_first_at":1600})
        self.assertEqual(result["confidence"],"low")
        self.assertNotIn("fixture",json.dumps(result))
        radios=[replace(r,observed_at=2000) for r in self.radios]
        quality={r.radio_id:dict(self.quality[r.radio_id],observed_at=2000,valid_until=3000,
                  incident_first_at=1600 if i<2 else None) for i,r in enumerate(radios)}
        missing=review_controller_load("w1",self.frame,self.snapshot,radios,quality,2000,history)
        self.assertEqual(missing["baseline"],"unavailable")
        with tempfile.TemporaryDirectory() as root:
            view=DiagnosticView(Store(root,mode="diagnostic"),lambda:2000)
            view.publish({"cluster":{"nodes":[{"controller":"w1","load_review":result}]}},[])
            self.assertEqual(view.page()["cluster"]["nodes"][0]["load_review"]["baseline_observation"],
                             result["baseline_observation"])
        self.frame["_field_connection"]="fixture:4"
        self.snapshot=replace(self.snapshot,field_connection="fixture:4")
        self.assertEqual(at(2060)["baseline"],"unavailable")
        self.assertTrue(all(k[0]>=2030 for k in history.pairs))
        history.clear()
        self.assertFalse(history.pairs)

    def test_history_bounds_clock_and_inventory(self):
        history=ControllerLoadHistory()
        history.context(("scope",),2000)
        dp={"observed_at":1000,"data":{"rows":[{"id":0,"type":"FP"}],
             "max_1s_percent":25,"max_4s_percent":20}}
        for t in range(1000,1300):
            history.record(t,30,dict(dp,observed_at=t))
        self.assertEqual(len(history.pairs),history.MAX_PAIRS)
        history.context(("scope",),4000)
        self.assertFalse(history.pairs)
        history.record(3990,30,dict(dp,observed_at=3990))
        history.context(("scope",),3999)
        self.assertFalse(history.pairs)
        history.record(3990,30,dict(dp,observed_at=3990))
        changed={"observed_at":3999,"data":dict(dp["data"],rows=[{"id":0,"type":"SP"}])}
        history.record(3999,35,changed)
        self.assertEqual(list(history.pairs),[(3999,3999)])
        history.record(3999,36,changed)
        self.assertIsNone(history.before_risk([4299],4300))

    def test_publication_masks_expired_candidate_without_mutating_evidence(self):
        self.quality[self.radios[-1].radio_id]["valid_until"]=1002
        candidate=self.review()
        clock=[1000]
        with tempfile.TemporaryDirectory() as root:
            view=DiagnosticView(Store(root,mode="diagnostic"),lambda:clock[0])
            view.publish({"cluster":{"nodes":[{"controller":"w1","load_review":candidate}]}},[])
            row=view.page()["cluster"]["nodes"][0]["load_review"]
            self.assertEqual(row["status"],"review_candidate")
            self.assertNotIn("valid_until",row)
            clock[0]=1002
            self.assertEqual(view.page()["cluster"]["nodes"][0]["load_review"],{"status":"not_evaluated","reason":"stale_or_paused"})
            self.assertEqual(candidate["valid_until"],1002)
