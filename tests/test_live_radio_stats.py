"""D03 protected integration; invented responses only."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from awqm.collector import command
from awqm.live_radio_stats import COMMAND, CONTRACT, CHECKS, load_stats_approvals
from awqm.jobs import DETAIL_COMMANDS
from awqm.worker import ControllerWorker, DETAIL_PARSERS
from awqm.engine import Engine
from awqm.storage import Store
from awqm.diagnostic_view import DiagnosticView
from awqm.manual_jobs import bind_ap
from awqm.ap_port_status import COMMAND as PORT
from awqm.arm_rf_summary import COMMAND as RF
from awqm.live_arm_history import COMMAND as ARM
from test_live_ap_ports import AP, MAC, KEY, Session, radio_approval, approval
from test_live_rf_summary import fixture as rf_fixture
from test_arm_history import LEGACY
from test_radio_stats import FIXTURE


class RadioStatsManifestTests(unittest.TestCase):
    def test_strict_command_and_separate_approval(self):
        self.assertEqual(command(COMMAND,(AP,0)),COMMAND+" ap-name "+AP+" radio 0 advanced")
        for target in (AP, (AP,True), (AP,"0"), (AP,-1), (AP,3), (AP,0,"advanced"),
                       [AP,0], ("bad;show version",0), (AP+"\n",0)):
            with self.subTest(target=target), self.assertRaises(ValueError):
                command(COMMAND,target)
        self.assertNotIn(COMMAND,DETAIL_COMMANDS)
        self.assertNotIn(COMMAND,DETAIL_PARSERS)
        with tempfile.TemporaryDirectory() as root:
            cfg=SimpleNamespace(root=Path(root),site="fixture")
            (cfg.root/"config").mkdir()
            path=cfg.root/"config/radio-stats-validation.json"
            radios={"w1":radio_approval()}
            self.assertEqual(load_stats_approvals(cfg,radios,1000),{})
            row={"controller":"w1","radio_tag":"tag-w1","approved_at":950,"valid_until":1900,
                 "reviewed_by":"fixture","checks":{k:True for k in CHECKS},
                 "targets":[{"ap_name":AP,"wired_mac":MAC}]}
            doc={"version":1,"enabled":True,"site":"fixture","contract":CONTRACT,"controllers":[row]}
            path.write_text(json.dumps(doc),encoding="utf-8")
            self.assertEqual(load_stats_approvals(cfg,radios,1000),{"w1":approval()})
            for invalid in ({**doc,"contract":"aos8-rf-summary-observation-v1"},
                            {**doc,"controllers":[{**row,"checks":{}}]},
                            {**doc,"controllers":[{**row,"valid_until":1000}]},
                            {**doc,"controllers":[{**row,"radio_tag":"wrong"}]}):
                path.write_text(json.dumps(invalid),encoding="utf-8")
                with self.assertRaises(ValueError):load_stats_approvals(cfg,radios,1000)


class RadioStatsWorkerTests(unittest.TestCase):
    def setUp(self):
        self.now=1000
        self.session=Session()
        self.session.responses[COMMAND]=FIXTURE.replace("TX Timeouts 4","TX Timeouts 18446744073709551615")
        self.session.responses[RF]=rf_fixture()
        self.session.responses[ARM]=LEGACY
        key=patch("awqm.worker.fingerprint",return_value=KEY)
        self.mock_key=key.start()
        self.addCleanup(key.stop)

    def worker(self, *, enabled=True, mode="standard", others=False):
        w=ControllerWorker("w1",lambda:self.session,lambda:self.now,lambda:self.now,
                           mode=mode,field_approval=radio_approval(),live_detail=True,
                           stats_approval=approval() if enabled else None,
                           port_approval=approval() if others else None,
                           rf_approval=approval() if others else None,
                           arm_approval=approval() if others else None)
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
        return [x for x in self.session.calls if x[0] in (COMMAND,PORT,RF,ARM)]

    def test_worker_projection_and_per_radio_cooldown(self):
        w=self.worker()
        r=w.tick()
        self.assertEqual((r.command,r.status,r.data,r.target),(COMMAND,"success",None,None))
        self.assertEqual(self.calls(),[(COMMAND,(AP,0),{"timeout":10,"idle_timeout":10})])
        frame=w.protection_frame()
        evidence=frame["radio_stats_evidence"]
        item=evidence["rows"][0]
        self.assertEqual(item["requested_radio"],0)
        self.assertEqual(item["counters"]["tx_timeouts"],"18446744073709551615")
        self.assertEqual(item["counters"]["radio_resets"],"0")
        self.assertIsNone(item["counters"]["tx_timeout_resets"])
        self.assertEqual(item["radio_percentages"]["channel_busy_4s_percent"],42.5)
        self.assertTrue(all(v is False for k,v in item.items() if k.endswith("_verified")))
        self.assertFalse(item["field_validated"])
        for secret in (MAC,bind_ap(MAC),"192.0.2.11"):
            self.assertNotIn(secret,json.dumps(frame))
        self.assertEqual(next(x for x in frame["cli_capabilities"] if x["id"]=="D03")["observation"],"parsed_response")
        with tempfile.TemporaryDirectory() as root:
            store=Store(Path(root)/"data",mode="diagnostic")
            engine=Engine([w],store,clock=lambda:self.now,wall=lambda:self.now)
            try:
                operations=engine.operation_status()
                view=DiagnosticView(store,lambda:self.now)
                view.publish({"operations":operations})
                self.assertEqual(view.operation_status()["state"]["controllers"][0]["radio_stats_evidence"],evidence)
                self.assertNotIn("radio_stats_evidence",json.dumps(view.page()))
                self.assertEqual(len(self.calls()),1)
                self.refresh(w,60)
                cmd="show ap radio-summary"
                self.change(w,cmd,data=[{**x,"utilization":90} for x in w.latest[cmd].data])
                self.assertEqual(w.tick().status,"success")
                self.assertEqual(self.calls()[-1][1],(AP,1))
                self.assertIsNone(w.tick())
                self.refresh(w,60)
                self.assertEqual(w.tick().status,"success")
                self.assertEqual(self.calls()[-1][1],(AP,0))
            finally:engine.close()

    def test_default_off_safe_wrong_key_stale_ambiguous_and_healthy(self):
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
        original=w.latest[cmd]
        self.change(w,cmd,observed_at=self.now-91)
        self.assertIsNone(w.tick())
        self.change(w,cmd,observed_at=self.now,data=[*original.data,original.data[0]])
        self.assertIsNone(w.tick())
        self.change(w,cmd,data=[{**x,"utilization":20,"interference":5,"noise":-94} for x in original.data])
        self.assertIsNone(w.tick())
        w.latest[cmd]=w.outcomes[cmd]=original
        active="show ap active details"
        a=w.latest[active]
        self.change(w,active,data=[{**x,"radios":[*x["radios"],x["radios"][0]]} for x in a.data])
        self.assertIsNone(w.tick())
        w.latest[active]=w.outcomes[active]=a
        w.schedule.protection.cpu(85,self.now)
        self.assertIsNone(w.tick())
        self.assertFalse(self.calls())

    def test_shared_limit_and_failure_withdrawal(self):
        w=self.worker(others=True)
        for i,cmd in enumerate((PORT,RF,ARM,COMMAND)):
            if i:self.refresh(w,60)
            self.assertEqual(w.tick().command,cmd)
            self.assertIsNone(w.tick())
        self.assertEqual([x[0] for x in self.calls()],[PORT,RF,ARM,COMMAND])
        self.assertTrue(w.protection_frame()["radio_stats_evidence"]["rows"])
        w.close()
        self.assertFalse(w.protection_frame()["radio_stats_evidence"]["rows"])

    def test_parse_failure_permission_latch(self):
        w=self.worker()
        self.assertEqual(w.tick().status,"success")
        self.refresh(w,120)
        self.session.responses[COMMAND]=FIXTURE+"invalid row\n"
        self.assertEqual(w.tick().status,"parse_error")
        self.assertFalse(w.protection_frame()["radio_stats_evidence"]["rows"])
        self.refresh(w,120)
        self.session.responses[COMMAND]="Permission denied"
        self.assertEqual(w.tick().status,"permission_denied")
        self.assertIn(COMMAND,w.detail_disabled)
        self.refresh(w,120)
        self.assertIsNone(w.tick())
        self.assertEqual(len(self.calls()),3)
