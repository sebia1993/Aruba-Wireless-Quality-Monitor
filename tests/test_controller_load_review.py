import json,tempfile,unittest
from dataclasses import replace
from awqm.aos8 import ControllerSnapshot
from awqm.controller_load_review import ControllerLoadHistory,review_controller_load,review_common_groups,CPU
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
    def test_common_group_requires_complete_disjoint_component_and_global_window(self):
        a,b,c,d,e=(f"{i:032x}" for i in range(1,6))
        def pair(x,y,start=970,end=1000):
            return {"incident_ids":sorted((x,y)),"window_start":start,"window_end":end}
        triangle=[pair(a,b),pair(b,c),pair(a,c)]
        separate=pair(d,e)
        expected={"groups":[{"incident_ids":[a,b,c],"pair_count":3,
                              "window_start":970,"window_end":1000},
                             {"incident_ids":[d,e],"pair_count":1,
                              "window_start":970,"window_end":1000}],
                  "withheld_components":0}
        self.assertEqual(review_common_groups(triangle+[separate]),expected)
        self.assertEqual(review_common_groups(list(reversed(triangle+[separate]))),expected)
        for pairs in ([pair(a,b),pair(b,c)],
                      triangle+[pair(c,d)],
                      [pair(a,b,970,990),pair(b,c,980,1000),pair(a,c,985,1001)]):
            with self.subTest(pairs=pairs):
                self.assertEqual(review_common_groups(pairs),
                                 {"groups":[],"withheld_components":1})
        self.assertEqual(review_common_groups([pair(a,b),pair(b,c),separate]),
                         {"groups":[expected["groups"][1]],"withheld_components":1})
        self.assertEqual(review_common_groups([]),{"groups":[],"withheld_components":0})

    def test_common_incident_pair_requires_reciprocal_aligned_evidence(self):
        a,b=self.radios[0],self.radios[-1]
        for i,r in enumerate(self.radios):
            self.quality[r.radio_id].update(incident_id=f"{i+1:032x}",incident_first_at=800)
        def edge(target,**changes):
            return dict(target_id=target.radio_id,target_context="fixture:3",target_radio_at=1000,
                candidate="RF05",confidence="medium",discovery="active",relation="same_block",
                band=target.band,channel=target.channel,width=40,overlap_mhz=40,
                neighbor_at=990,target_until=1010) | changes
        def publication(rows):
            return dict(radio_at=1000,context="fixture:3",observed_at=1000,
                        until=1100,truncated=False,rows=rows)
        views={a.radio_id:publication([edge(b)]),b.radio_id:publication([edge(a)])}
        def evaluate(v):
            return review_controller_load("w1",self.frame,self.snapshot,self.radios,self.quality,
                                          self.now,neighbor_views=v)
        result=evaluate(views)
        common=result["common_incident_review"]
        self.assertEqual(common["status"],"review_pairs")
        self.assertEqual(common["pairs"],[{"incident_ids":[self.quality[a.radio_id]["incident_id"],
            self.quality[b.radio_id]["incident_id"]],"rf_candidate":"RF05","overlap_mhz":40,
            "window_start":970,"window_end":1000}])
        self.assertEqual(common["group_review"],{"groups":[{"incident_ids":common["pairs"][0]["incident_ids"],
            "pair_count":1,"window_start":970,"window_end":1000}],"withheld_components":0})
        self.assertEqual(common["valid_until"],1010)
        self.assertNotIn(a.radio_id,json.dumps(common))
        self.assertNotIn("fixture:3",json.dumps(common))
        for changes in ({"neighbor_at":969},{"candidate":None},{"target_context":"other"},
                        {"target_until":1000},{"width":20},{"discovery":"passive"}):
            with self.subTest(changes=changes):
                bad={**views,b.radio_id:publication([edge(a,**changes)])}
                self.assertEqual(evaluate(bad)["common_incident_review"]["status"],"not_evaluated")
        self.assertEqual(evaluate({a.radio_id:views[a.radio_id]})["common_incident_review"]["status"],"not_evaluated")
        bad={**views,b.radio_id:publication([edge(a),edge(a,overlap_mhz=20)])}
        self.assertEqual(evaluate(bad)["common_incident_review"]["status"],"not_evaluated")
        self.quality[b.radio_id]["incident_first_at"]=831
        self.assertEqual(evaluate(views)["common_incident_review"]["status"],"not_evaluated")
        self.quality[b.radio_id]["incident_first_at"]=800
        with tempfile.TemporaryDirectory() as root:
            clock=[1000]
            view=DiagnosticView(Store(root,mode="diagnostic"),lambda:clock[0])
            view.publish({"cluster":{"nodes":[{"controller":"w1","load_review":result}]}},[])
            public=view.page()["cluster"]["nodes"][0]["load_review"]
            self.assertEqual(public["common_incident_review"]["pairs"],common["pairs"])
            self.assertEqual(public["common_incident_review"]["group_review"],common["group_review"])
            self.assertNotIn("valid_until",public["common_incident_review"])
            clock[0]=1010
            public=view.page()["cluster"]["nodes"][0]["load_review"]
            self.assertEqual(public["status"],"review_candidate")
            self.assertEqual(public["common_incident_review"],{"status":"not_evaluated","reason":"stale_or_paused"})
            self.assertEqual(common["valid_until"],1010)

    def test_common_pair_from_live_neighbor_and_committed_ledger(self):
        from awqm.quality_ledger import QualityLedger
        from awqm.live_neighbors import LiveNeighbors,NeighborApproval,COMMAND
        from awqm.rf_neighbors import ChannelBlock
        from datetime import datetime,timezone
        a,b=self.radios[0],self.radios[-1]
        a=replace(a,ap_name="AP1",epoch="w1:5GHz:AP:44+")
        b=replace(b,ap_name="AP2",channel=48,epoch="w1:5GHz:AP:48-")
        blocks={("5GHz","44+"):ChannelBlock("5GHz",44,(44,48),True),
                ("5GHz","48-"):ChannelBlock("5GHz",48,(44,48),True)}
        with tempfile.TemporaryDirectory() as root:
            store=Store(root,mode="diagnostic")
            ledger=QualityLedger(store)
            for t in (1000,1180,1360):
                radios=[replace(r,observed_at=t) for r in (a,b)]
                snap=ControllerSnapshot("w1",t,[],[
                    {"name":"AP1","ip":"192.0.2.1","roles":["A"]},
                    {"name":"AP2","ip":"192.0.2.2","roles":["A"]}],[],
                    field_tag=self.tag,field_until=2000,field_connection="fixture:3")
                store.save_batch(radios)
                quality=ledger.evaluate(radios,[snap],t,{r.radio_id:t for r in radios})
            self.assertTrue(all(q["status"]=="PERSISTENT" for q in quality.values()))
            bss=("02:ff:00:01:00:01","02:ff:00:02:00:01")
            frames={"w1":(snap,Reading("show ap bss-table","success",1360,[
                {"bssid":mac,"ap_name":r.ap_name,"ip":f"192.0.2.{i+1}",
                 "band":r.band,"channel":r.channel,"width":"40MHz","service":True}
                for i,(r,mac) in enumerate(zip(radios,bss))],generation=3))}
            neighbors=LiveNeighbors({"w1":NeighborApproval(self.tag,900,1900,True,0,blocks)},
                                    ledger.incidents,key=b"fixture")
            index=neighbors.index(radios,[snap],frames,1360)
            self.assertEqual(set(index),set(bss))
            stamp=datetime.fromtimestamp(1360,timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            for i in (0,1):
                j=1-i;target=radios[j]
                data=[{"bssid":bss[j],"band":target.band,"primary_channel":target.channel,
                       "width_candidate":40,"channel_raw":target.epoch.rsplit(":",1)[-1],
                       "snr":35,"discovery":"active","last_update_raw":stamp}]
                reading=Reading(COMMAND,"success",1365,data,generation=3,target=bss[i])
                neighbors.ingest(reading,"w1",snap,index,quality,1365)
            views=neighbors.publish(radios,[snap],quality,1365)
            self.frame["resource_validation"]["until"]=1900
            self.frame["readings"][CPU]=replace(self.frame["readings"][CPU],observed_at=1335)
            self.frame["readings"][DATAPATH]=replace(self.frame["readings"][DATAPATH],observed_at=1360)
            result=review_controller_load("w1",self.frame,snap,radios,quality,1365,
                                          neighbor_views=views)
            common=result["common_incident_review"]
            self.assertEqual(common["status"],"review_pairs")
            ids=sorted(q["incident_id"] for q in quality.values())
            self.assertEqual(common["pairs"][0]["incident_ids"],ids)
            view=DiagnosticView(store,lambda:1365,incidents=ledger.incidents)
            view.publish({"cluster":{"nodes":[{"controller":"w1","load_review":result}]}},
                         [(r.radio_id,r.observed_at) for r in radios],quality=quality,
                         neighbor_views=views,neighbor_enabled=True)
            page=view.page()
            self.assertEqual(page["cluster"]["nodes"][0]["load_review"]["common_incident_review"]["pairs"],
                             common["pairs"])
            self.assertNotIn("fixture:3",json.dumps(page))
            self.assertNotIn(a.radio_id,json.dumps(page))
            self.assertNotIn("fixture:3",json.dumps(view.neighbors(view.token(a.radio_id))))
            group=page["cluster"]["nodes"][0]["load_review"]["common_incident_review"]["group_review"]
            self.assertEqual(group,{"groups":[{"incident_ids":ids,"pair_count":1,
                "window_start":1335,"window_end":1365}],"withheld_components":0})
            for ident in ids:
                self.assertEqual(ledger.incidents.detail(ident)["state"],"PERSISTENT")
            # Exercise the real validated pair -> immutable SQLite parent path.
            from awqm.common_incidents import CommonIncidents,validate_links,backup_matches
            from awqm.maintenance import verified_backup,restore_new
            from awqm.retention import _incident_reference_holds,readonly
            from pathlib import Path
            from copy import deepcopy
            with tempfile.TemporaryDirectory() as dst:
                p=Path(dst)
                before=verified_backup(store,p/"before",include_aggregates=False)
                parents=CommonIncidents(store)
                invalid=deepcopy(result)
                invalid["common_incident_review"]["group_review"]["groups"][0]["pair_count"]=2
                self.assertEqual(parents.capture("w1",invalid,1365),[])
                self.assertEqual(parents.capture("w2",result,1365),[])
                self.assertEqual(parents.capture("w1",result,result["common_incident_review"]["valid_until"]),[])
                # Neighbor evidence is committed five seconds after the
                # Radio observation, at the exact end of the shared window.
                # A revision outside that window or about an older sample
                # must still be rejected, even when capture itself is later.
                with store.lock,store._db(store.root/"core.db") as db:
                    row=db.execute("""SELECT e.revision,e.at,e.evidence FROM rf_incident_events e
                        JOIN rf_incidents i ON i.incident_id=e.incident_id
                        AND i.evidence_rev=e.revision WHERE i.incident_id=?""",(ids[0],)).fetchone()
                    rev,event_at,payload=row
                    self.assertEqual(event_at,1365)
                    self.assertEqual(json.loads(payload)["observed_at"],1360)
                    db.execute("UPDATE rf_incident_events SET at=1366 WHERE incident_id=? AND revision=?",(ids[0],rev))
                self.assertEqual(parents.capture("w1",result,1366),[])
                with store.lock,store._db(store.root/"core.db") as db:
                    db.execute("UPDATE rf_incident_events SET at=1359 WHERE incident_id=? AND revision=?",(ids[0],rev))
                self.assertEqual(parents.capture("w1",result,1365),[])
                with store.lock,store._db(store.root/"core.db") as db:
                    stale=json.dumps(dict(json.loads(payload),observed_at=1359),sort_keys=True)
                    db.execute("UPDATE rf_incident_events SET at=?,evidence=? WHERE incident_id=? AND revision=?",
                               (event_at,stale,ids[0],rev))
                    db.execute("UPDATE rf_incidents SET evidence=? WHERE incident_id=?",(stale,ids[0]))
                self.assertEqual(parents.capture("w1",result,1365),[])
                with store.lock,store._db(store.root/"core.db") as db:
                    db.execute("UPDATE rf_incident_events SET evidence=? WHERE incident_id=? AND revision=?",(payload,ids[0],rev))
                    db.execute("UPDATE rf_incidents SET evidence=? WHERE incident_id=?",(payload,ids[0]))
                created=parents.capture("w1",result,1365)
                self.assertEqual(len(created),1)
                self.assertEqual(parents.capture("w1",result,1365),created)
                self.assertEqual(CommonIncidents(store).capture("w1",result,1365),created)
                with store.lock,store._db(store.root/"core.db") as db,readonly(before/"core.db") as old:
                    validate_links(db)
                    self.assertFalse(backup_matches(db,old,ids[0]))
                    self.assertEqual(_incident_reference_holds(db,set(ids)),set(ids))
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM rf_common_incidents").fetchone(),(1,))
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM rf_common_incident_children").fetchone(),(2,))
                    db.execute("UPDATE rf_incident_events SET at=1366 WHERE incident_id=? AND revision=?",(ids[0],rev))
                    with self.assertRaisesRegex(ValueError,"common incident child revision unavailable"):
                        validate_links(db)
                    db.execute("UPDATE rf_incident_events SET at=? WHERE incident_id=? AND revision=?",(event_at,ids[0],rev))
                    validate_links(db)
                # Historical reads and retention/restore must revalidate the
                # original capture contract, not merely trust matching JSON.
                with store.lock,store._db(store.root/"core.db") as db:
                    original=db.execute("SELECT evidence FROM rf_common_incidents WHERE parent_id=?",(created[0],)).fetchone()[0]
                    for changes in ({"cpu_percent":69},{"cpu_percent":True},
                                    {"datapath_max_1s_percent":101},{"datapath_at":1366},
                                    {"cpu_at":1334}):
                        with self.subTest(resource=changes):
                            bad=json.dumps(dict(json.loads(original),**changes),sort_keys=True)
                            db.execute("UPDATE rf_common_incidents SET evidence=? WHERE parent_id=?",(bad,created[0]))
                            with self.assertRaisesRegex(ValueError,"common incident resource evidence invalid"):
                                validate_links(db)
                    db.execute("UPDATE rf_common_incidents SET evidence=? WHERE parent_id=?",(original,created[0]))
                    original_child=db.execute("SELECT radio_id,controller,first_at FROM rf_incidents WHERE incident_id=?",(ids[1],)).fetchone()
                    other_radio=db.execute("SELECT radio_id FROM rf_incidents WHERE incident_id=?",(ids[0],)).fetchone()[0]
                    duplicate_ap=other_radio.rsplit(":",1)[0]+":99"
                    for radio,controller,first in ((duplicate_ap,original_child[1],original_child[2]),
                            (original_child[0],"other-controller",original_child[2]),
                            (original_child[0],original_child[1],0)):
                        with self.subTest(identity=(radio,controller,first)):
                            db.execute("UPDATE rf_incidents SET radio_id=?,controller=?,first_at=? WHERE incident_id=?",
                                       (radio,controller,first,ids[1]))
                            with self.assertRaisesRegex(ValueError,"common incident child identity mismatch"):
                                validate_links(db)
                            db.execute("UPDATE rf_incidents SET radio_id=?,controller=?,first_at=? WHERE incident_id=?",
                                       (*original_child,ids[1]))
                    validate_links(db)
                    bad=json.dumps(dict(json.loads(original),cpu_percent=69),sort_keys=True)
                    db.execute("UPDATE rf_common_incidents SET evidence=? WHERE parent_id=?",(bad,created[0]))
                self.assertEqual(parents.capture("w1",result,1365),[])
                self.assertEqual(view.incident_detail(ids[0])["common_incident_lifecycle"]["status"],"unavailable")
                invalid_backup=verified_backup(store,p/"invalid-resource",include_aggregates=False)
                with store.lock,store._db(store.root/"core.db") as db,readonly(invalid_backup/"core.db") as copy:
                    self.assertFalse(backup_matches(db,copy,ids[0]))
                    db.execute("UPDATE rf_common_incidents SET evidence=? WHERE parent_id=?",(original,created[0]))
                with self.assertRaisesRegex(ValueError,"common incident resource evidence invalid"):
                    restore_new(invalid_backup,p/"invalid-resource-restore")
                self.assertFalse((p/"invalid-resource-restore").exists())
                self.assertEqual(parents.capture("w1",result,1365),created)
                # The authenticated detail path only reports recorded child
                # recovery. It never promotes an open or missing observation.
                def lifecycle():
                    return view.incident_detail(ids[0])["common_incident_lifecycle"]
                self.assertEqual(lifecycle()["status"],"not_all_resolved_observed")
                self.assertEqual(lifecycle()["resolved_count"],0)
                self.assertEqual(lifecycle()["parent_id"],created[0])
                for t in (1420,1480,1540,1600,1660,1720):
                    current=[replace(a,observed_at=t,utilization=20,interference=2,noise=-95),
                             replace(b,observed_at=t,utilization=20 if t>=1600 else 90,
                                     interference=2 if t>=1600 else 30,noise=-95)]
                    store.save_batch(current)
                    ledger.evaluate(current,[replace(snap,observed_at=t)],t,
                                    {r.radio_id:t for r in current})
                    if t==1540:
                        partial=lifecycle()
                        self.assertEqual((partial["status"],partial["resolved_count"]),
                                         ("not_all_resolved_observed",1))
                        self.assertEqual(partial["last_resolved_at"],1540)
                done=lifecycle()
                self.assertEqual((done["status"],done["child_count"],done["resolved_count"]),
                                 ("all_children_resolved_observed",2,2))
                self.assertEqual((done["first_resolved_at"],done["last_resolved_at"]),(1540,1720))
                self.assertEqual(done["service_impact"],"unverified")
                self.assertFalse(done["verified"])
                self.assertNotIn(a.radio_id,json.dumps(done))
                # Even a RESOLVED snapshot cannot prove recovery if its
                # exact terminal event is missing or inconsistent.
                with store.lock,store._db(store.root/"core.db") as db:
                    terminal=db.execute("SELECT evidence_rev,ended_at FROM rf_incidents WHERE incident_id=?",
                                        (ids[0],)).fetchone()
                    db.execute("UPDATE rf_incident_events SET at=? WHERE incident_id=? AND revision=?",
                               (terminal[1]+1,ids[0],terminal[0]))
                self.assertEqual(lifecycle()["status"],"unavailable")
                self.assertIsNone(lifecycle()["resolved_count"])
                with store.lock,store._db(store.root/"core.db") as db:
                    db.execute("UPDATE rf_incident_events SET at=? WHERE incident_id=? AND revision=?",
                               (terminal[1],ids[0],terminal[0]))
                    self.assertEqual(_incident_reference_holds(db,set(ids)),set(ids))
                    validate_links(db)
                self.assertEqual(lifecycle(),done)
                saved=verified_backup(store,p/"saved",include_aggregates=False)
                restored=restore_new(saved,p/"restored")
                with store.lock,store._db(store.root/"core.db") as db,readonly(restored/"core.db") as copy:
                    validate_links(copy)
                    self.assertTrue(backup_matches(db,copy,ids[0]))
                    db.execute("UPDATE rf_common_incident_children SET evidence_rev=99999 WHERE child_id=?",(ids[0],))
                corrupt=verified_backup(store,p/"corrupt",include_aggregates=False)
                with self.assertRaisesRegex(ValueError,"common incident membership mismatch"):
                    restore_new(corrupt,p/"refused")
                self.assertFalse((p/"refused").exists())

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
            # The publication TTL (20 s) is independent of the candidate
            # evidence lifetime. Exercise both boundaries without relaxing either.
            self.assertGreater(result["valid_until"],2021)
            clock[0]=2020
            self.assertEqual(view.page()["cluster"]["nodes"][0]["load_review"]["rf_baseline_observation"],
                             result["rf_baseline_observation"])
            clock[0]=2021
            stale=view.page()
            self.assertFalse(stale["available"])
            self.assertIsNone(stale["cluster"])
            # The coordinator may republish the same still-valid evidence.
            # Keep the publication fresh to isolate the evidence expiry guard.
            clock[0]=result["valid_until"]-1
            view.publish({"cluster":{"nodes":[{"controller":"w1","load_review":result}]}},[])
            self.assertEqual(view.page()["cluster"]["nodes"][0]["load_review"]["rf_baseline_observation"],
                             result["rf_baseline_observation"])
            clock[0]=result["valid_until"]
            expired=view.page()
            self.assertTrue(expired["available"])
            self.assertEqual(expired["cluster"]["nodes"][0]["load_review"],
                             {"status":"not_evaluated","reason":"stale_or_paused"})
            self.assertEqual(result["rf_baseline"],"observed")
            self.assertEqual(result["rf_baseline_observation"]["radio_count"],3)
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

    def test_current_radio_conflicts_withhold_candidate_and_block_replay(self):
        history=ControllerLoadHistory()
        self.frame["resource_validation"].update(approved_at=100,until=3000)
        def at(t,mode="valid",h=history):
            snapshot=replace(self.snapshot,observed_at=t,field_until=3000)
            self.frame["readings"][CPU]=replace(self.frame["readings"][CPU],observed_at=t-10,data=82)
            self.frame["readings"][DATAPATH]=replace(self.frame["readings"][DATAPATH],observed_at=t)
            radios=[replace(r,observed_at=t) for r in self.radios]
            quality={r.radio_id:dict(self.quality[r.radio_id],observed_at=t,valid_until=3000,
                                     incident_first_at=900) for r in radios}
            if mode=="duplicate":radios.append(radios[0])
            if mode=="duplicate_conflict":radios.append(replace(radios[0],utilization=91))
            if mode=="changed":radios[0]=replace(radios[0],utilization=91)
            return review_controller_load("w1",self.frame,snapshot,radios,quality,t,h)
        blocked={"status":"not_evaluated","reason":"radio_observation_unusable"}
        first=at(1000)
        self.assertEqual(first["status"],"review_candidate")
        self.assertEqual(at(1000,"duplicate"),first)
        # Conflicting observations in the same risk bucket are not equivalent.
        self.assertEqual(at(1000,"changed"),blocked)
        self.assertFalse(history.radio_frames)
        self.assertEqual(at(1000),blocked)
        self.assertEqual(at(1010,"duplicate_conflict"),blocked)
        self.assertEqual(at(1010),blocked)
        self.assertEqual(at(1020)["status"],"review_candidate")
        self.assertEqual(at(1020)["rf_baseline"],"unavailable")
        # The same-frame check is also enforced without optional history.
        self.assertEqual(at(1030,"duplicate_conflict",None),blocked)
        self.assertEqual(at(1030,"duplicate",None)["radio_count"],3)
        with tempfile.TemporaryDirectory() as root:
            view=DiagnosticView(Store(root,mode="diagnostic"),lambda:1030)
            view.publish({"cluster":{"nodes":[{"controller":"w1","load_review":blocked}]}},[])
            self.assertEqual(view.page()["cluster"]["nodes"][0]["load_review"],blocked)
            self.assertNotIn("00:11",json.dumps(view.page()["cluster"]))

    def test_over_capacity_radio_frame_cannot_be_a_partial_cause_candidate(self):
        from unittest.mock import patch
        history=ControllerLoadHistory()
        with patch.object(ControllerLoadHistory,"MAX_RADIOS_PER_FRAME",2):
            self.assertEqual(self.review(),{"status":"not_evaluated","reason":"radio_observation_unusable"})
            result=review_controller_load("w1",self.frame,self.snapshot,self.radios,self.quality,self.now,history)
            self.assertEqual(result,{"status":"not_evaluated","reason":"radio_observation_unusable"})
            self.assertFalse(history.radio_frames)
        # Same frame must not be accepted once discarded.
        self.assertEqual(review_controller_load("w1",self.frame,self.snapshot,self.radios,self.quality,self.now,history),result)

    def test_same_cohort_after_recovery_with_real_ledger(self):
        from awqm.quality_ledger import QualityLedger
        history=ControllerLoadHistory()
        self.frame["resource_validation"].update(approved_at=100,until=3000)
        with tempfile.TemporaryDirectory() as root:
            store=Store(root,mode="diagnostic")
            ledger=QualityLedger(store)
            def at(t,risk):
                snapshot=replace(self.snapshot,observed_at=t,field_until=3000)
                self.frame["readings"][CPU]=replace(self.frame["readings"][CPU],observed_at=t-10,data=82 if risk else 40)
                self.frame["readings"][DATAPATH]=replace(self.frame["readings"][DATAPATH],observed_at=t)
                radios=[replace(r,observed_at=t,utilization=90 if risk else 20,
                    interference=30 if risk else 2,noise=-95) for r in self.radios]
                for r in radios:store.save(r)
                q=ledger.evaluate(radios,[snapshot],t,{r.radio_id:t for r in radios})
                return review_controller_load("w1",self.frame,snapshot,radios,q,t,history)
            for t in (1000,1060):self.assertEqual(at(t,True)["status"],"not_evaluated")
            candidate=at(1120,True)
            self.assertEqual(candidate["status"],"review_candidate")
            for t in (1180,1240,1300):self.assertEqual(at(t,False)["status"],"not_evaluated")
            self.assertEqual(at(1300,False)["status"],"not_evaluated")
            result=at(1360,False)
            self.assertEqual((result["status"],result["resolved_at"],result["radio_count"]),
                ("recovery_observation",1300,3))
            self.assertEqual((result["risk_observation"]["cpu_percent"],result["cpu_percent"]),(82,40))
            self.assertEqual(result["risk_observation"]["radio_at"],1120)
            self.assertFalse(result["verified"])
            self.assertNotIn("00:11",json.dumps(result))
            self.assertNotIn("private",json.dumps(result))
            clock=[1360]
            view=DiagnosticView(store,lambda:clock[0])
            view.publish({"cluster":{"nodes":[{"controller":"w1","load_review":result}]}},[])
            self.assertEqual(view.page()["cluster"]["nodes"][0]["load_review"]["status"],"recovery_observation")
            # Refresh the publication separately from the evidence expiry.
            clock[0]=result["valid_until"]-1
            view.publish({"cluster":{"nodes":[{"controller":"w1","load_review":result}]}},[])
            self.assertEqual(view.page()["cluster"]["nodes"][0]["load_review"]["status"],"recovery_observation")
            clock[0]+=1
            self.assertEqual(view.page()["cluster"]["nodes"][0]["load_review"]["reason"],"stale_or_paused")
            for t in (1420,1480,1540,1600):
                self.assertEqual(at(t,False)["status"],"recovery_observation")
            self.assertEqual(at(1601,False)["status"],"not_evaluated")

    def test_recovery_rejects_missing_staggered_changed_or_new_incidents(self):
        self.frame["resource_validation"].update(approved_at=100,until=3000)
        def at(h,t,status,mode=None,resource_offset=10):
            snapshot=replace(self.snapshot,observed_at=t,field_until=3000,field_profile="standard")
            self.frame["readings"][CPU]=replace(self.frame["readings"][CPU],
                observed_at=t-resource_offset,data=82 if status=="PERSISTENT" else 40)
            self.frame["readings"][DATAPATH]=replace(self.frame["readings"][DATAPATH],observed_at=t)
            radios=[replace(r,observed_at=t,utilization=90 if status=="PERSISTENT" else 20,
                    interference=30 if status=="PERSISTENT" else 2,noise=-95,
                    epoch="changed" if mode=="epoch" and i==0 else r.epoch) for i,r in enumerate(self.radios)]
            if mode=="missing":radios=radios[:-1]
            quality={r.radio_id:dict(self.quality[r.radio_id],observed_at=t,valid_until=3000,
                status=("RECOVERING" if mode=="staggered" and i==0 else status),
                incident_id=None if status=="NORMAL" else ("%032x"%(i+1)),
                incident_first_at=900) for i,r in enumerate(radios)}
            if mode=="new":quality[radios[0].radio_id]["incident_id"]="f"*32
            return review_controller_load("w1",self.frame,snapshot,radios,quality,t,h)
        for mode in ("missing","staggered","epoch","new"):
            with self.subTest(mode=mode):
                h=ControllerLoadHistory()
                self.assertEqual(at(h,1000,"PERSISTENT")["status"],"review_candidate")
                self.assertEqual(at(h,1060,"RESOLVED",mode)["status"],"not_evaluated")
                if mode!="staggered":self.assertIsNone(h.recovery)
                # A partially resolved cohort must never infer the missing
                # explicit RESOLVED event from a later NORMAL sample.
                self.assertEqual(at(h,1120,"NORMAL")["status"],"not_evaluated")
                self.assertIsNone(h.recovery)
        h=ControllerLoadHistory()
        self.assertEqual(at(h,1000,"PERSISTENT")["status"],"review_candidate")
        self.assertEqual(at(h,1060,"RESOLVED")["status"],"not_evaluated")
        # A cached CPU reading at the resolution boundary is not post-risk.
        self.assertEqual(at(h,1080,"NORMAL",resource_offset=20)["status"],"not_evaluated")
        self.assertEqual(at(h,1090,"NORMAL")["status"],"recovery_observation")
        self.assertEqual(at(h,1190,"NORMAL")["status"],"not_evaluated")
        self.assertIsNone(h.recovery)

    def test_staggered_recovery_preserves_original_cohort_with_real_ledger(self):
        from awqm.quality_ledger import QualityLedger
        history=ControllerLoadHistory()
        self.frame["resource_validation"].update(approved_at=100,until=3000)
        with tempfile.TemporaryDirectory() as root:
            store=Store(root,mode="diagnostic")
            ledger=QualityLedger(store)
            def at(t,risk_indices,cpu,dp_offset=0):
                snapshot=replace(self.snapshot,observed_at=t,field_until=3000,
                    field_profile="standard")
                self.frame["readings"][CPU]=replace(self.frame["readings"][CPU],
                    observed_at=t-10,data=cpu)
                self.frame["readings"][DATAPATH]=replace(self.frame["readings"][DATAPATH],
                    observed_at=t+dp_offset)
                radios=[replace(r,observed_at=t,utilization=90 if i in risk_indices else 20,
                    interference=30 if i in risk_indices else 2,noise=-95)
                    for i,r in enumerate(self.radios)]
                for r in radios:store.save(r)
                q=ledger.evaluate(radios,[snapshot],t+dp_offset,{r.radio_id:t for r in radios})
                return review_controller_load("w1",self.frame,snapshot,radios,q,t+dp_offset,history)
            for t in (1000,1060):self.assertEqual(at(t,{0,1,2},82)["status"],"not_evaluated")
            original=at(1120,{0,1,2},82)
            self.assertEqual(original["radio_count"],3)
            # Radio 0 clears earlier, while the other two still qualify
            # as a current multi-AP candidate. Never shrink the old cohort.
            for t in (1180,1240,1300):
                remaining=at(t,{1,2},82)
                self.assertEqual((remaining["status"],remaining["radio_count"]),("review_candidate",2))
                self.assertEqual(len(history.recovery["cohort"]),3)
            self.assertEqual(list(history.recovery["resolved"].values()),[1300])
            for t in (1360,1420,1480):
                self.assertEqual(at(t,set(),40)["status"],"not_evaluated")
            self.assertEqual(at(1480,set(),40)["status"],"not_evaluated")
            result=at(1540,set(),40)
            self.assertEqual((result["status"],result["first_resolved_at"],
                result["resolved_at"],result["radio_count"]),("recovery_observation",1300,1480,3))
            self.assertEqual(result["risk_observation"]["radio_at"],1120)
            self.assertFalse(result["verified"])
            self.assertNotIn("00:11",json.dumps(result))
            view=DiagnosticView(store,lambda:1540)
            view.publish({"cluster":{"nodes":[{"controller":"w1","load_review":result}]}},[])
            self.assertEqual(view.page()["cluster"]["nodes"][0]["load_review"]["first_resolved_at"],1300)
            for t in (1600,1660,1720):
                self.assertEqual(at(t,set(),40)["status"],"recovery_observation")
            # Aligned but after the five-minute resource window.
            self.assertEqual(at(1780,set(),40,dp_offset=1)["status"],"not_evaluated")
            self.assertEqual(at(1781,set(),40)["status"],"not_evaluated")

    def test_staggered_recovery_rejects_recurrence_and_lost_context(self):
        def row(status,ident,epoch="e",clear=True):
            return (epoch,"5GHz",44,not clear,clear,20 if clear else 90,
                    2 if clear else 30,-95,status,ident)
        ids={k:"%032x"%i for i,k in enumerate(("a","b","c"),1)}
        risk={k:row("PERSISTENT",v,clear=False) for k,v in ids.items()}
        public={"cpu_percent":82,"cpu_at":990,"datapath_at":1000,
                "datapath_max_1s_percent":25,"datapath_max_4s_percent":20,
                "radio_at":1000,"ap_count":2,"radio_count":3}
        early={"a":row("RESOLVED",ids["a"]),"b":risk["b"],"c":risk["c"]}
        for mode in ("recur","missing","epoch","unclear","wrong_id","gap"):
            with self.subTest(mode=mode):
                h=ControllerLoadHistory()
                h.remember_risk(public,risk,set(risk))
                self.assertIsNone(h.after_recovery(early,{},public,1060,3000,90))
                self.assertEqual(h.recovery["resolved"],{"a":1060})
                following={"a":row("NORMAL",None),"b":row("RESOLVED",ids["b"]),
                           "c":row("RESOLVED",ids["c"])}
                t=1120
                if mode=="recur":following["a"]=row("DETECTED","f"*32,clear=False)
                if mode=="missing":del following["a"]
                if mode=="epoch":following["a"]=row("NORMAL",None,epoch="changed")
                if mode=="unclear":following["a"]=row("WATCH",None,clear=False)
                if mode=="wrong_id":following["b"]=row("RESOLVED","f"*32)
                if mode=="gap":t=1151
                self.assertIsNone(h.after_recovery(following,{},public,t,3000,90))
                self.assertIsNone(h.recovery)
