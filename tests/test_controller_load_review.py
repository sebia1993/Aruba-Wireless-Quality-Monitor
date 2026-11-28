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

    def test_independent_resource_timestamp_conflicts_block_replayed_pairs(self):
        history=ControllerLoadHistory()
        history.context(("scope",),1200)
        rows=[{"id":0,"type":"FP","percent_1s":25,"percent_4s":20},
              {"id":1,"type":"FP","percent_1s":10,"percent_4s":5}]
        def dp(at,other=10):
            return {"observed_at":at,"data":{"rows":[rows[0],dict(rows[1],percent_1s=other)],
                "max_1s_percent":25,"max_4s_percent":20}}
        self.assertTrue(history.record(1000,40,dp(1010)))
        self.assertTrue(history.record(1000,40,dp(1020)))
        self.assertFalse(history.record(1000,41,dp(1030)))
        self.assertFalse(history.pairs)
        self.assertIsNone(history.before_risk([1400],1400))
        self.assertFalse(history.record(1000,40,dp(1040)))
        self.assertFalse(history.record(1040,40,dp(1030)))
        self.assertTrue(history.record(1040,40,dp(1050)))
        self.assertTrue(history.record(1045,41,dp(1050)))
        # Same Datapath timestamp and same maxima but contradictory CPU row.
        self.assertFalse(history.record(1049,42,dp(1050,other=11)))
        self.assertFalse(history.record(1060,42,dp(1050)))
        self.assertTrue(history.record(1060,42,dp(1070)))
        public=history.before_risk([1400],1400)
        self.assertEqual(public["cpu_at"],1060)
        self.assertNotIn("_datapath_fingerprint",public)
        history.context(("changed",),1200)
        self.assertTrue(history.record(1000,40,dp(1010)))

    def test_live_review_withholds_conflicting_resource_and_requires_fresh_pair(self):
        history=ControllerLoadHistory()
        self.frame["resource_validation"].update(approved_at=100,until=3000)
        def at(t,cpu_at,cpu_value,dp_at):
            snapshot=replace(self.snapshot,observed_at=t,field_until=3000)
            self.frame["readings"][CPU]=replace(self.frame["readings"][CPU],observed_at=cpu_at,data=cpu_value)
            self.frame["readings"][DATAPATH]=replace(self.frame["readings"][DATAPATH],observed_at=dp_at)
            radios=[replace(r,observed_at=t) for r in self.radios]
            quality={r.radio_id:dict(self.quality[r.radio_id],observed_at=t,valid_until=3000,
                                     incident_first_at=900) for r in radios}
            return review_controller_load("w1",self.frame,snapshot,radios,quality,t,history)
        self.assertEqual(at(1000,990,70,1000)["status"],"review_candidate")
        conflict=at(1010,990,71,1010)
        self.assertEqual(conflict,{"status":"not_evaluated","reason":"resource_observation_conflict"})
        self.assertEqual(at(1015,990,70,1015),conflict)
        self.assertEqual(at(1020,1010,70,1020),conflict)
        result=at(1030,1020,70,1030)
        self.assertEqual(result["status"],"review_candidate")
        self.assertEqual(result["baseline"],"unavailable")
        with tempfile.TemporaryDirectory() as root:
            view=DiagnosticView(Store(root,mode="diagnostic"),lambda:1010)
            view.publish({"cluster":{"nodes":[{"controller":"w1","load_review":conflict}]}},[])
            self.assertEqual(view.page()["cluster"]["nodes"][0]["load_review"],conflict)
        # An approval lifetime or reading generation change starts a new
        # bounded segment; it must not reuse the previous resource history.
        self.frame["resource_validation"]["until"]=2900
        self.assertEqual(at(1040,1035,70,1040)["status"],"review_candidate")
        self.assertEqual(list(history.pairs),[(1035,1040)])
        for command in (CPU,DATAPATH):
            self.frame["readings"][command]=replace(self.frame["readings"][command],generation=4)
        self.assertEqual(at(1050,1045,70,1050)["status"],"review_candidate")
        self.assertEqual(list(history.pairs),[(1045,1050)])

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

    def test_exact_prior_radio_cohort_comparison(self):
        self.frame["resource_validation"].update(approved_at=100,until=3000)
        def evaluate(history,t,prior=False,missing=False,changed=False,skew=30):
            snapshot=replace(self.snapshot,observed_at=t,field_until=3000)
            self.frame["readings"][CPU]=replace(self.frame["readings"][CPU],
                observed_at=t-skew,data=42 if prior else 82)
            self.frame["readings"][DATAPATH]=replace(self.frame["readings"][DATAPATH],observed_at=t)
            radios=[replace(r,observed_at=t,epoch="different" if changed and i==0 else r.epoch,
                utilization=20 if prior and i<2 else 90,
                interference=2 if prior and i<2 else 30) for i,r in enumerate(self.radios)]
            if missing:radios=radios[:-1]
            quality={r.radio_id:dict(self.quality[r.radio_id],observed_at=t,valid_until=3000,
                status="NORMAL" if prior and i<2 else "PERSISTENT",
                incident_id=None if prior and i<2 else "private",incident_first_at=1600)
                for i,r in enumerate(radios)}
            return review_controller_load("w1",self.frame,snapshot,radios,quality,t,history)
        history=ControllerLoadHistory()
        self.assertEqual(evaluate(history,1300,prior=True)["reason"],"cpu_screen_not_met")
        result=evaluate(history,2000)
        self.assertEqual(result["rf_baseline"],"observed")
        self.assertEqual(result["rf_baseline_observation"],{"radio_at":1300,"radio_count":3,
            "prior_positive_risk_radio_count":1,"prior_policy_normal_radio_count":2,
            "current_positive_risk_radio_count":3})
        self.assertEqual(result["confidence"],"low")
        self.assertNotIn("00:11",json.dumps(result));self.assertNotIn("private",json.dumps(result))
        with tempfile.TemporaryDirectory() as root:
            clock=[2000]
            view=DiagnosticView(Store(root,mode="diagnostic"),lambda:clock[0])
            view.publish({"cluster":{"nodes":[{"controller":"w1","load_review":result}]}},[])
            self.assertEqual(view.page()["cluster"]["nodes"][0]["load_review"]["rf_baseline_observation"],
                             result["rf_baseline_observation"])
            clock[0]=result["valid_until"]
            self.assertEqual(view.page()["cluster"]["nodes"][0]["load_review"]["status"],"not_evaluated")
        # Same resource comparison remains available, but do not substitute
        # an older apparently favorable cohort for a missing or changed one.
        for mode in ("missing","changed","outside"):
            with self.subTest(mode=mode):
                history=ControllerLoadHistory()
                evaluate(history,1260,prior=True)
                evaluate(history,1300 if mode!="outside" else 1301,prior=True,
                         missing=mode=="missing",changed=mode=="changed")
                if mode=="outside":
                    # Use a fresh history containing only a pair outside the
                    # five-minute upper boundary.
                    history=ControllerLoadHistory()
                    evaluate(history,1301,prior=True)
                result=evaluate(history,2000)
                self.assertEqual(result["rf_baseline"],"unavailable")
                self.assertIsNone(result["rf_baseline_observation"])
                self.assertEqual(result["confidence"],"low")

    def test_radio_history_bounds_alignment_conflict_and_scope(self):
        history=ControllerLoadHistory()
        history.context(("scope",),1400)
        prior={"private-radio":("epoch","5GHz",44,False,True)}
        current={"private-radio":("epoch","5GHz",44,True,False)}
        baseline={"risk_first_at":1600,"cpu_at":1270,"datapath_at":1300}
        self.assertTrue(history.record_radio_snapshot(1269,prior))
        self.assertIsNone(history.before_risk_radio(baseline,current))
        self.assertTrue(history.record_radio_snapshot(1270,prior))
        self.assertEqual(history.before_risk_radio(baseline,current)["radio_at"],1270)
        self.assertTrue(history.record_radio_snapshot(1300,prior))
        self.assertEqual(history.before_risk_radio(baseline,current)["radio_at"],1300)
        self.assertFalse(history.record_radio_snapshot(1300,current))
        self.assertFalse(history.radio_frames)
        self.assertFalse(history.record_radio_snapshot(1299,prior))
        self.assertTrue(history.record_radio_snapshot(1301,prior))
        self.assertIsNone(history.before_risk_radio(baseline,current))
        for t in range(1302,1400):history.record_radio_snapshot(t,prior)
        self.assertEqual(len(history.radio_frames),history.MAX_RADIO_FRAMES)
        self.assertFalse(history.record_radio_snapshot(1400,{str(i):prior["private-radio"]
            for i in range(history.MAX_RADIOS_PER_FRAME+1)}))
        self.assertFalse(history.radio_frames)
        history.context(("changed",),1400)
        self.assertTrue(history.record_radio_snapshot(1300,prior))
        history.context(("changed",),3200)
        self.assertFalse(history.radio_frames)
