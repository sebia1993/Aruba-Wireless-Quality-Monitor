"""Protected D04 synthetic worker/manifest and shared D07 scheduling regressions."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from awqm.arm_rf_summary import COMMAND
from awqm.ap_port_status import COMMAND as PORT
from awqm.live_rf_summary import load_rf_approvals, CHECKS, CONTRACT
from awqm.live_ap_ports import PortApproval
from awqm.jobs import DETAIL_COMMANDS
from awqm.manual_jobs import bind_ap
from awqm.worker import ControllerWorker, DETAIL_PARSERS
from awqm.engine import Engine
from awqm.storage import Store
from awqm.diagnostic_view import DiagnosticView
from test_live_ap_ports import AP, MAC, KEY, Session, radio_approval, approval
from test_arm_rf_summary import capture, ROW


def fixture():
    return capture(ROW.replace("36 0 ", "36 18446744073709551615 ",1),
                   "44 N/A 0 N/A N/A N/A N/A 2/3//4/5(9)")


class RFManifestTests(unittest.TestCase):
    def test_separate_default_off_and_strict_scope(self):
        with tempfile.TemporaryDirectory() as root:
            cfg=SimpleNamespace(root=Path(root),site="fixture")
            (cfg.root/"config").mkdir()
            path=cfg.root/"config/rf-summary-validation.json"
            radios={"w1":radio_approval()}
            self.assertEqual(load_rf_approvals(cfg,radios,1000),{})
            row={"controller":"w1","radio_tag":"tag-w1","approved_at":950,"valid_until":1900,
                 "reviewed_by":"fixture","checks":{k:True for k in CHECKS},
                 "targets":[{"ap_name":AP,"wired_mac":MAC}]}
            doc={"version":1,"enabled":True,"site":"fixture","contract":CONTRACT,"controllers":[row]}
            path.write_text(json.dumps(doc),encoding="utf-8")
            self.assertEqual(load_rf_approvals(cfg,radios,1000),{"w1":approval()})
            for invalid in ({**doc,"contract":"aos8-ap-port-observation-v1"},
                            {**doc,"controllers":[{**row,"checks":{}}]},
                            {**doc,"controllers":[{**row,"valid_until":2001}]},
                            {**doc,"controllers":[{**row,"targets":[{"ap_name":"bad;cli","wired_mac":MAC}]}]}):
                path.write_text(json.dumps(invalid),encoding="utf-8")
                with self.assertRaises(ValueError):load_rf_approvals(cfg,radios,1000)
            self.assertNotIn(COMMAND,DETAIL_COMMANDS)
            self.assertNotIn(COMMAND,DETAIL_PARSERS)


class RFWorkerTests(unittest.TestCase):
    def setUp(self):
        self.now=1000
        self.session=Session()
        self.session.responses[COMMAND]=fixture()
        key=patch("awqm.worker.fingerprint",return_value=KEY)
        self.mock_key=key.start()
        self.addCleanup(key.stop)

    def worker(self, *, enabled=True, port=False, mode="standard"):
        w=ControllerWorker("w1",lambda:self.session,lambda:self.now,lambda:self.now,
                           mode=mode,field_approval=radio_approval(),live_detail=True,
                           port_approval=approval() if port else None,
                           rf_approval=approval() if enabled else None)
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

    def calls(self):
        return [x for x in self.session.calls if x[0] in (COMMAND,PORT)]

    def test_worker_and_sanitized_admin_view(self):
        w=self.worker()
        reading=w.tick()
        self.assertEqual((reading.command,reading.status,reading.data,reading.target),(COMMAND,"success",None,None))
        self.assertEqual(self.calls(),[(COMMAND,AP,{"timeout":10,"idle_timeout":10})])
        frame=w.protection_frame()
        evidence=frame["rf_summary_evidence"]
        item=evidence["rows"][0]
        self.assertEqual(item["received_at"],1000)
        self.assertEqual(item["channels"][0]["retry_raw"],"18446744073709551615")
        self.assertEqual(item["channels"][0]["noise_raw"],"92")
        self.assertIsNone(item["channels"][0]["reported_band"])
        self.assertIsNone(item["channels"][1]["retry_raw"])
        self.assertFalse(item["channels"][1]["interference_index"]["total_consistent"])
        self.assertFalse(item["field_validated"])
        self.assertTrue(all(v is False for k,v in item.items() if k.endswith("_verified")))
        self.assertNotIn(MAC,json.dumps(frame))
        self.assertNotIn(bind_ap(MAC),json.dumps(frame))
        self.assertNotIn("192.0.2.11",json.dumps(frame))
        d04=next(x for x in frame["cli_capabilities"] if x["id"]=="D04")
        self.assertEqual(d04["observation"],"parsed_response")
        self.assertFalse(d04["field_support_verified"])
        with tempfile.TemporaryDirectory() as root:
            store=Store(Path(root)/"data",mode="diagnostic")
            engine=Engine([w],store,clock=lambda:self.now,wall=lambda:self.now)
            try:
                operations=engine.operation_status()
                self.assertEqual(operations["controllers"][0]["rf_summary_evidence"],evidence)
                view=DiagnosticView(store,lambda:self.now)
                view.publish({"operations":operations})
                self.assertNotIn("rf_summary_evidence",json.dumps(view.page()))
                self.assertEqual(view.operation_status()["state"],operations)
                self.assertEqual(len(self.calls()),1)
            finally:engine.close()

    def test_round_cooldown_shared_and_fair(self):
        w=self.worker(port=True)
        self.assertEqual(w.tick().command,PORT)
        self.assertIsNone(w.tick())
        self.refresh(w,60)
        self.assertEqual(w.tick().command,COMMAND)
        self.refresh(w,60)
        self.assertEqual(w.tick().command,PORT)
        self.refresh(w,60)
        self.assertIsNone(w.tick())
        self.refresh(w,60)
        self.assertEqual(w.tick().command,COMMAND)
        self.assertEqual([x[0] for x in self.calls()],[PORT,COMMAND,PORT,COMMAND])

    def test_trigger_and_anchor_checks(self):
        w=self.worker()
        cmd="show ap radio-summary"
        original=w.latest[cmd]
        self.change(w,cmd,data=[{**r,"utilization":20,"interference":5,"noise":-94} for r in original.data])
        self.assertIsNone(w.tick())
        self.change(w,cmd,data=[*original.data,original.data[0]])
        self.assertIsNone(w.tick())
        self.change(w,cmd,data=original.data,observed_at=self.now-91)
        self.assertIsNone(w.tick())
        w.latest[cmd]=w.outcomes[cmd]=original
        self.mock_key.return_value="wrong"
        self.assertIsNone(w.tick())
        self.mock_key.return_value=KEY
        w.schedule.protection.cpu(85,self.now)
        self.assertIsNone(w.tick())
        self.assertFalse(self.calls())

    def test_no_implicit_enable_and_no_safe_profile(self):
        w=self.worker(enabled=False)
        self.assertIsNone(w.tick())
        w.close()
        w=self.worker(mode="safe")
        self.assertIsNone(w.tick())
        self.assertFalse(self.calls())

    def test_bound_and_invalidation(self):
        w=self.worker()
        self.session.responses[COMMAND]=capture(*(ROW.replace("36 ",str(ch)+" ",1) for ch in range(1,66)))
        self.assertEqual(w.tick().status,"success")
        item=w.protection_frame()["rf_summary_evidence"]["rows"][0]
        self.assertEqual((item["row_count"],len(item["channels"]),item["truncated"]),(65,64,True))
        self.refresh(w,90)
        cmd="show ap radio-summary"
        rd=w.latest[cmd]
        self.change(w,cmd,data=[{**r,"utilization":20,"interference":5,"noise":-94} for r in rd.data])
        self.assertFalse(w.protection_frame()["rf_summary_evidence"]["rows"])
        w.latest[cmd]=w.outcomes[cmd]=rd
        self.assertFalse(w.protection_frame()["rf_summary_evidence"]["rows"])
        self.refresh(w,90)
        self.session.responses[COMMAND]="Permission denied"
        self.assertEqual(w.tick().status,"permission_denied")
        self.assertIn(COMMAND,w.detail_disabled)
        self.assertFalse(w.protection_frame()["rf_summary_evidence"]["rows"])
        self.refresh(w,180)
        self.assertIsNone(w.tick())
        self.assertEqual(len(self.calls()),2)
