"""D08 protected worker integration with invented CLI responses."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from awqm.live_arm_history import COMMAND, CONTRACT, CHECKS, load_arm_approvals
from awqm.jobs import DETAIL_COMMANDS
from awqm.worker import ControllerWorker, DETAIL_PARSERS
from awqm.engine import Engine
from awqm.storage import Store
from awqm.diagnostic_view import DiagnosticView
from awqm.manual_jobs import bind_ap
from awqm.ap_port_status import COMMAND as PORT
from awqm.arm_rf_summary import COMMAND as RF
from test_live_ap_ports import AP, MAC, KEY, Session, radio_approval, approval
from test_live_rf_summary import fixture as rf_fixture
from test_arm_history import LEGACY, MODERN, TABLE, ROWS, LEGACY_LEGEND


class ARMManifestTests(unittest.TestCase):
    def test_independent_approval(self):
        with tempfile.TemporaryDirectory() as root:
            cfg=SimpleNamespace(root=Path(root),site="fixture")
            (cfg.root/"config").mkdir()
            path=cfg.root/"config/arm-history-validation.json"
            radios={"w1":radio_approval()}
            self.assertEqual(load_arm_approvals(cfg,radios,1000),{})
            row={"controller":"w1","radio_tag":"tag-w1","approved_at":950,"valid_until":1900,
                 "reviewed_by":"fixture","checks":{k:True for k in CHECKS},
                 "targets":[{"ap_name":AP,"wired_mac":MAC}]}
            doc={"version":1,"enabled":True,"site":"fixture","contract":CONTRACT,"controllers":[row]}
            path.write_text(json.dumps(doc),encoding="utf-8")
            self.assertEqual(load_arm_approvals(cfg,radios,1000),{"w1":approval()})
            for invalid in ({**doc,"contract":"aos8-rf-summary-observation-v1"},
                            {**doc,"controllers":[{**row,"checks":{}}]},
                            {**doc,"controllers":[{**row,"valid_until":1000}]},
                            {**doc,"controllers":[{**row,"targets":[{"ap_name":"bad;cli","wired_mac":MAC}]}]}):
                path.write_text(json.dumps(invalid),encoding="utf-8")
                with self.assertRaises(ValueError):load_arm_approvals(cfg,radios,1000)
            self.assertNotIn(COMMAND,DETAIL_COMMANDS)
            self.assertNotIn(COMMAND,DETAIL_PARSERS)


class ARMWorkerTests(unittest.TestCase):
    def setUp(self):
        self.now=1000
        self.session=Session()
        self.session.responses[COMMAND]=LEGACY
        self.session.responses[RF]=rf_fixture()
        key=patch("awqm.worker.fingerprint",return_value=KEY)
        self.mock_key=key.start()
        self.addCleanup(key.stop)

    def worker(self, *, enabled=True, mode="standard", others=False):
        w=ControllerWorker("w1",lambda:self.session,lambda:self.now,lambda:self.now,
                           mode=mode,field_approval=radio_approval(),live_detail=True,
                           arm_approval=approval() if enabled else None,
                           port_approval=approval() if others else None,
                           rf_approval=approval() if others else None)
        self.addCleanup(w.close)
        for _ in range(len(w.schedule.polls)):
            self.assertEqual(w.tick().status,"success")
        return w

    def change(self,w,cmd,**changes):
        w.latest[cmd]=w.outcomes[cmd]=replace(w.latest[cmd],**changes)

    def refresh(self,w,elapsed):
        self.now+=elapsed
        for cmd in tuple(w.latest):
            self.change(w,cmd,observed_at=self.now)
        w.schedule.due={k:self.now+1000 for k in w.schedule.polls}
        w.schedule.protection.cpu(20,self.now)

    def healthy(self,w,channel=44):
        cmd="show ap radio-summary"
        self.change(w,cmd,data=[{**r,"utilization":20,"interference":5,"noise":-94,
                     **({"channel":channel,"mode":"AP:"+str(channel)+"+"} if r["band"]=="5GHz" else {})}
                    for r in w.latest[cmd].data])
        cmd="show ap active details"
        self.change(w,cmd,data=[{**a,"radios":[{**r,**({"channel":channel} if r["band"]=="5GHz" else {})}
                    for r in a["radios"]]} for a in w.latest[cmd].data])

    def calls(self):
        return [x for x in self.session.calls if x[0] in (COMMAND,PORT,RF)]

    def test_worker_and_sanitized_admin_view(self):
        w=self.worker()
        r=w.tick()
        self.assertEqual((r.command,r.status,r.data,r.target),(COMMAND,"success",None,None))
        self.assertEqual(self.calls(),[(COMMAND,AP,{"timeout":10,"idle_timeout":10})])
        frame=w.protection_frame()
        evidence=frame["arm_history_evidence"]
        item=evidence["rows"][0]
        self.assertEqual(item["received_at"],1000)
        self.assertEqual(item["event_count"],4)
        self.assertEqual(item["interfaces"][0]["events"][0]["reported_age_seconds"],62)
        self.assertIsNone(item["interfaces"][0]["phy_type"])
        self.assertTrue(all(v is False for k,v in item.items() if k.endswith("_verified")))
        self.assertFalse(item["field_validated"])
        self.assertNotIn(MAC,json.dumps(frame))
        self.assertNotIn(bind_ap(MAC),json.dumps(frame))
        self.assertNotIn("192.0.2.11",json.dumps(frame))
        self.assertNotIn("event_at",json.dumps(item))
        self.assertEqual(next(x for x in frame["cli_capabilities"] if x["id"]=="D08")["observation"],"parsed_response")
        with tempfile.TemporaryDirectory() as root:
            store=Store(Path(root)/"data",mode="diagnostic")
            engine=Engine([w],store,clock=lambda:self.now,wall=lambda:self.now)
            try:
                operations=engine.operation_status()
                view=DiagnosticView(store,lambda:self.now)
                view.publish({"operations":operations})
                self.assertEqual(view.operation_status()["state"]["controllers"][0]["arm_history_evidence"],evidence)
                self.assertNotIn("arm_history_evidence",json.dumps(view.page()))
                self.assertEqual(len(self.calls()),1)
            finally:engine.close()

    def test_channel_change_not_healthy_or_replayed(self):
        w=self.worker()
        self.refresh(w,60)
        self.healthy(w)
        self.assertIsNone(w.tick())
        self.refresh(w,60)
        self.healthy(w,48)
        self.assertEqual(w.tick().command,COMMAND)
        self.assertEqual(len(self.calls()),1)
        self.refresh(w,180)
        self.assertIsNone(w.tick())
        self.assertEqual(len(self.calls()),1)

    def test_shared_round_limit_and_fairness(self):
        w=self.worker(others=True)
        self.assertEqual(w.tick().command,PORT)
        self.assertIsNone(w.tick())
        self.refresh(w,60)
        self.assertEqual(w.tick().command,RF)
        self.refresh(w,60)
        self.assertEqual(w.tick().command,COMMAND)
        self.assertIsNone(w.tick())
        self.assertEqual([x[0] for x in self.calls()],[PORT,RF,COMMAND])

    def test_default_off_safe_mode_wrong_key_stale_and_cpu(self):
        w=self.worker(enabled=False)
        self.assertIsNone(w.tick())
        w.close()
        w=self.worker(mode="safe")
        self.assertIsNone(w.tick())
        w.close()
        w=self.worker()
        self.mock_key.return_value="wrong"
        self.assertIsNone(w.tick())
        self.mock_key.return_value=KEY
        cmd="show ap radio-summary"
        rd=w.latest[cmd]
        self.change(w,cmd,data=[*rd.data,rd.data[0]])
        self.assertIsNone(w.tick())
        self.change(w,cmd,data=rd.data,observed_at=self.now-91)
        self.assertIsNone(w.tick())
        w.latest[cmd]=w.outcomes[cmd]=rd
        w.schedule.protection.cpu(85,self.now)
        self.assertIsNone(w.tick())
        self.assertFalse(self.calls())

    def test_bounded_header_only_denial_and_invalidation(self):
        w=self.worker()
        self.session.responses[COMMAND]="Interface :wifi0\n"+TABLE+ROWS*25+LEGACY_LEGEND
        self.assertEqual(w.tick().status,"success")
        item=w.protection_frame()["arm_history_evidence"]["rows"][0]
        self.assertEqual((item["event_count"],len(item["interfaces"][0]["events"]),item["truncated"]),(75,64,True))
        self.refresh(w,180)
        self.session.responses[COMMAND]=MODERN
        self.assertEqual(w.tick().status,"success")
        item=w.protection_frame()["arm_history_evidence"]["rows"][0]
        self.assertEqual(item["event_count"],0)
        self.assertFalse(item["completeness_verified"])
        self.assertFalse(item["interfaces"][0]["table_present"])
        self.refresh(w,180)
        self.session.responses[COMMAND]="Permission denied"
        self.assertEqual(w.tick().status,"permission_denied")
        self.assertIn(COMMAND,w.detail_disabled)
        self.assertFalse(w.protection_frame()["arm_history_evidence"]["rows"])
        self.refresh(w,180)
        self.assertIsNone(w.tick())
        self.assertEqual(len(self.calls()),3)
