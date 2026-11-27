"""D07 service worker tests with synthetic CLI and no network."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from awqm.field_validation import Approval
from awqm.fixture_session import FixtureSession
from awqm.jobs import DETAIL_COMMANDS
from awqm.live_ap_ports import PortApproval, load_port_approvals, CHECKS, CONTRACT
from awqm.manual_jobs import bind_ap
from awqm.ap_port_status import COMMAND
from awqm.worker import ControllerWorker
from awqm.engine import Engine
from awqm.diagnostic_view import DiagnosticView
from awqm.storage import Store

AP = "DEMO-AP-001"
MAC = "02:00:00:00:00:01"
KEY = "fixture-key"
HEADER = "Port MAC Type Forward Mode Admin Oper Speed Duplex 802.3az PoE STP TX-Packets TX-Bytes RX-Packets RX-Bytes"
ROW = "0 02:00:00:00:00:01 GE N/A enabled up 2.5 Gb/s full N/A N/A N/A 0 18446744073709551615 N/A 300"


def response():
    return "\n".join([f'AP "{AP}" Port Status (updated every 60 seconds)', "-"*50,
                      HEADER, " ".join(["----"]*15), ROW])


def radio_approval(controller="w1", ip="198.51.100.1"):
    return Approval(controller,ip,"Aruba7240XM","8.10.0.24",KEY,900,2000,"tag-"+controller)


def approval():
    return PortApproval("tag-w1",950,1900,((AP,bind_ap(MAC)),))


class Session(FixtureSession):
    def __init__(self):
        super().__init__(1)
        self.calls=[]
        self.responses["show version"]="ArubaOS (MODEL: Aruba7240XM), Version 8.10.0.24"
        self.responses[COMMAND]=response()

    def run(self,name,target=None,**kwargs):
        self.calls.append((name,target,kwargs))
        return super().run(name,target=target,**kwargs)


class PortManifestTests(unittest.TestCase):
    def test_disabled_default_and_strict_approval(self):
        with tempfile.TemporaryDirectory() as root:
            cfg=SimpleNamespace(root=Path(root),site="fixture")
            (cfg.root/"config").mkdir()
            path=cfg.root/"config/ap-port-validation.json"
            radios={"w1":radio_approval(),"w2":radio_approval("w2","198.51.100.2")}
            self.assertEqual(load_port_approvals(cfg,radios,1000),{})
            row={"controller":"w1","radio_tag":"tag-w1","approved_at":950,"valid_until":1900,
                 "reviewed_by":"fixture","checks":{k:True for k in CHECKS},
                 "targets":[{"ap_name":AP,"wired_mac":MAC}]}
            doc={"version":1,"enabled":True,"site":"fixture","contract":CONTRACT,"controllers":[row]}
            path.write_text(json.dumps(doc),encoding="utf-8")
            self.assertEqual(load_port_approvals(cfg,radios,1000),{"w1":approval()})
            for mutation in (
                {"targets":[{"ap_name":"AP;show version","wired_mac":MAC}]},
                {"checks":{k:False for k in CHECKS}}, {"radio_tag":"other"},
                {"valid_until":2001}, {"valid_until":1000},
                {"targets":[{"ap_name":AP,"wired_mac":MAC}]*2},
            ):
                with self.subTest(mutation=mutation):
                    doc["controllers"]=[{**row,**mutation}]
                    path.write_text(json.dumps(doc),encoding="utf-8")
                    with self.assertRaises(ValueError):load_port_approvals(cfg,radios,1000)
            doc["controllers"]=[row,{**row,"controller":"w2","radio_tag":"tag-w2"}]
            path.write_text(json.dumps(doc),encoding="utf-8")
            with self.assertRaises(ValueError):load_port_approvals(cfg,radios,1000)
            self.assertNotIn(COMMAND,DETAIL_COMMANDS)


class PortWorkerTests(unittest.TestCase):
    def setUp(self):
        self.now=1000
        self.session=Session()
        key=patch("awqm.worker.fingerprint",return_value=KEY)
        self.mock_key=key.start()
        self.addCleanup(key.stop)

    def worker(self, *, enabled=True, mode="standard"):
        w=ControllerWorker("w1",lambda:self.session,lambda:self.now,lambda:self.now,
                           mode=mode,field_approval=radio_approval(),live_detail=True,
                           port_approval=approval() if enabled else None)
        self.addCleanup(w.close)
        for _ in range(len(w.schedule.polls)):
            self.assertEqual(w.tick().status,"success")
        return w

    def port_calls(self):
        return [x for x in self.session.calls if x[0]==COMMAND]

    def change(self,w,cmd,**changes):
        w.latest[cmd]=w.outcomes[cmd]=replace(w.latest[cmd],**changes)

    def refresh(self,w,elapsed=120):
        self.now+=elapsed
        for cmd in tuple(w.latest):
            self.change(w,cmd,observed_at=self.now)
        w.schedule.due={k:self.now+1000 for k in w.schedule.polls}
        w.schedule.protection.cpu(20,self.now)

    def test_worker_tick_and_admin_projection(self):
        w=self.worker()
        reading=w.tick()
        self.assertEqual((reading.command,reading.status),(COMMAND,"success"))
        self.assertIsNone(reading.target)
        self.assertIsNone(reading.data)
        self.assertEqual(self.port_calls(),[(COMMAND,AP,{"timeout":10,"idle_timeout":10})])
        frame=w.protection_frame()
        evidence=frame["ap_port_evidence"]
        self.assertEqual(evidence["selected_count"],1)
        record=evidence["rows"][0]
        self.assertEqual(record["received_at"],1000)
        self.assertEqual(record["ports"][0]["speed_mbps"],2500)
        self.assertEqual(record["ports"][0]["tx_packets"],"0")
        self.assertEqual(record["ports"][0]["tx_bytes"],"18446744073709551615")
        self.assertIsNone(record["ports"][0]["rx_packets"])
        self.assertNotIn(MAC,json.dumps(frame))
        self.assertNotIn(bind_ap(MAC),json.dumps(frame))
        self.assertNotIn("192.0.2.11",json.dumps(frame))
        for flag in ("physical_uplink_verified","sample_time_verified","row_count_verified","field_validated"):
            self.assertFalse(record[flag])
        d07=next(x for x in frame["cli_capabilities"] if x["id"]=="D07")
        self.assertEqual(d07["observation"],"parsed_response")
        self.assertFalse(d07["field_support_verified"])
        self.assertIsNone(w.tick())
        self.assertEqual(len(self.port_calls()),1)
        with tempfile.TemporaryDirectory() as root:
            store=Store(Path(root)/"data",mode="diagnostic")
            engine=Engine([w],store,clock=lambda:self.now,wall=lambda:self.now)
            try:
                operations=engine.operation_status()
                self.assertEqual(operations["controllers"][0]["ap_port_evidence"],evidence)
                view=DiagnosticView(store,lambda:self.now)
                view.publish({"operations":operations})
                self.assertEqual(view.operation_status()["state"],operations)
                self.assertNotIn("ap_port_evidence",json.dumps(view.page()))
                self.assertEqual(len(self.port_calls()),1)
            finally:engine.close()

    def test_reported_port_boundary_identity_precision_and_failure(self):
        w=self.worker()
        baseline=response().replace(" N/A 300", " 10 300")
        changed_mac="02:00:00:00:00:09"
        first_mac=MAC
        low=baseline.replace("18446744073709551615","18446744073709551614")
        changed=low.replace(first_mac,changed_mac)
        self.session.responses[COMMAND]=baseline
        for index, (raw, expected) in enumerate((
                (baseline, "not_assessed"),
                (low, "reported_decrease"),
                (low, "no_boundary_observed"),
                (changed, "reported_port_context_changed"),
                (changed.replace("2.5 Gb/s","1 Gb/s"), "reported_port_context_changed"),
                (changed+"\ninvalid trailing row", None),
                (changed, "not_assessed"))):
            if index:
                self.refresh(w)
            self.session.responses[COMMAND]=raw
            reading=w.tick()
            frame=w.protection_frame()
            rows=frame["ap_port_evidence"]["rows"]
            if expected is None:
                self.assertEqual(reading.status,"parse_error")
                self.assertEqual(rows,[])
                continue
            self.assertEqual(reading.status,"success")
            self.assertEqual(rows[0]["ports"][0]["counter_boundary_hint"],expected)
            self.assertFalse(rows[0]["sample_time_verified"])
            self.assertFalse(rows[0]["physical_uplink_verified"])
            for private in (first_mac,changed_mac,bind_ap(changed_mac)):
                self.assertNotIn(private,json.dumps(frame))
        self.assertEqual(len(self.port_calls()),7)

    def test_reported_port_boundary_unknown_on_missing_gap_or_approval_change(self):
        w=self.worker()
        # One counter is absent in the original fixture; monotonic remaining
        # reported counts cannot establish an all-counter no-boundary hint.
        self.assertEqual(w.tick().status,"success")
        self.refresh(w)
        self.assertEqual(w.tick().status,"success")
        def hint():
            return w.protection_frame()["ap_port_evidence"]["rows"][0]["ports"][0]["counter_boundary_hint"]
        self.assertEqual(hint(),"not_assessed")
        self.session.responses[COMMAND]=response().replace(" N/A 300"," 10 300")
        self.refresh(w,121)
        self.assertEqual(w.tick().status,"success")
        self.assertEqual(hint(),"not_assessed")
        self.refresh(w)
        w.ap_ports.approval=replace(w.ap_ports.approval,valid_until=1800)
        self.assertEqual(w.tick().status,"success")
        self.assertEqual(hint(),"not_assessed")
        self.assertEqual(len(self.port_calls()),4)

    def test_default_off_and_safe_mode(self):
        w=self.worker(enabled=False)
        self.assertIsNone(w.tick())
        self.assertFalse(self.port_calls())
        w.close()
        w=self.worker(mode="safe")
        self.assertIsNone(w.tick())
        self.assertFalse(self.port_calls())

    def test_key_anchor_identity_cpu_and_queue_fail_closed(self):
        w=self.worker()
        self.mock_key.return_value="wrong"
        self.assertIsNone(w.tick())
        self.mock_key.return_value=KEY
        inv="show ap database long"
        original=w.latest[inv]
        self.change(w,inv,data=[*original.data,{**original.data[0],"name":"OTHER"}])
        self.assertIsNone(w.tick())
        self.change(w,inv,data=[{**original.data[0],"switch_ip":"198.51.100.9"}])
        self.assertIsNone(w.tick())
        w.latest[inv]=w.outcomes[inv]=original
        job=w.submit_detail("existing","show ap debug client-table",AP,binding=bind_ap(MAC))
        self.assertIsNone(w.tick())
        self.assertEqual(w.jobs.get(job.job_id).status,"deferred")
        w.jobs.cancel(job.job_id,self.now)
        w.schedule.protection.account(self.now-10,self.now,True)
        self.assertIsNone(w.tick())
        w.schedule.protection.cpu(85,self.now)
        self.assertIsNone(w.tick())
        self.assertFalse(self.port_calls())

    def test_denial_latches_across_reconnect(self):
        w=self.worker()
        self.session.responses[COMMAND]="Permission denied\n"
        self.assertEqual(w.tick().status,"permission_denied")
        self.assertIn(COMMAND,w.detail_disabled)
        self.assertEqual(w.protection_frame()["ap_port_evidence"]["rows"],[])
        self.assertTrue(w.quiesce_collection(False))
        for _ in range(len(w.schedule.polls)):
            self.assertEqual(w.tick().status,"success")
        self.refresh(w)
        self.assertIsNone(w.tick())
        self.assertEqual(len(self.port_calls()),1)

    def test_failed_parse_and_generation_invalidate_observation(self):
        w=self.worker()
        self.assertEqual(w.tick().status,"success")
        self.assertTrue(w.protection_frame()["ap_port_evidence"]["rows"])
        w.generation+=1
        self.assertFalse(w.protection_frame()["ap_port_evidence"]["rows"])
        w.generation-=1
        self.assertFalse(w.protection_frame()["ap_port_evidence"]["rows"])
        self.refresh(w)
        self.session.responses[COMMAND]=response()+"\ninvalid trailing row"
        self.assertEqual(w.tick().status,"parse_error")
        self.assertFalse(w.protection_frame()["ap_port_evidence"]["rows"])
        self.assertNotIn(COMMAND,w.detail_disabled)
        self.assertEqual(len(self.port_calls()),2)

    def test_expiry_and_transport_failure(self):
        w=self.worker()
        self.assertEqual(w.tick().status,"success")
        self.refresh(w,901)
        self.assertIsNone(w.tick())
        self.assertFalse(w.protection_frame()["ap_port_evidence"]["rows"])
        self.assertEqual(len(self.port_calls()),1)
        # Reset the synthetic clock in a separate worker, then fail transport.
        w.close()
        self.now=1000
        w=self.worker()
        with patch.object(self.session,"run",side_effect=OSError("private error")):
            self.assertEqual(w.tick().status,"transport_error")
        self.assertIsNone(w.session)
        self.assertFalse(w.protection_frame()["ap_port_evidence"]["rows"])
        self.assertNotIn("private error",json.dumps(w.protection_frame()))

    def test_peer_identity_veto_and_recovery_without_extra_ssh(self):
        w=self.worker()
        peer=ControllerWorker("w2",lambda:FixtureSession(2),lambda:self.now,lambda:self.now,mode="standard")
        self.addCleanup(peer.close)
        # Real worker parser path populates both peer frames independently
        # of the Radio snapshot used for quality publication.
        for _ in peer.schedule.polls:
            self.assertEqual(peer.tick().status,"success")
        inv="show ap database long"
        active="show ap active details"
        row=w.latest[inv].data[0]
        claim=w.latest[active].data[0]
        with tempfile.TemporaryDirectory() as root:
            engine=Engine([w,peer],Store(root,mode="diagnostic"),clock=lambda:self.now,wall=lambda:self.now)
            try:
                peer._record(inv,"success",[dict(row,name="RENAMED")])
                self.assertIsNone(w.tick())
                self.assertFalse(self.port_calls())
                peer._record(inv,"success",[dict(row)])
                peer._record(active,"success",[dict(claim)])
                self.assertIsNone(w.tick())
                self.assertFalse(self.port_calls())
                peer._record(active,"success",[dict(claim,roles=["SA"])])
                self.assertEqual(w.tick().status,"success")
                self.assertTrue(w.protection_frame()["ap_port_evidence"]["rows"])
                # Positive peer evidence withdraws the current admin view.
                peer._record(inv,"success",[dict(row,ip="192.0.2.99")])
                self.assertFalse(w.protection_frame()["ap_port_evidence"]["rows"])
                self.refresh(w)
                self.assertIsNone(w.tick())
                self.assertEqual(len(self.port_calls()),1)
                # A failed peer refresh is not a positive contradiction.
                peer._record(inv,"parse_error")
                self.assertEqual(w.tick().status,"success")
                self.assertEqual(len(self.port_calls()),2)
            finally:engine.close()

    def test_peer_change_during_selected_command_discards_response(self):
        w=self.worker()
        peer=ControllerWorker("w2",lambda:FixtureSession(2),lambda:self.now,lambda:self.now,mode="standard")
        self.addCleanup(peer.close)
        row=w.latest["show ap database long"].data[0]
        with tempfile.TemporaryDirectory() as root:
            engine=Engine([w,peer],Store(root,mode="diagnostic"),clock=lambda:self.now,wall=lambda:self.now)
            try:
                run=self.session.run
                def contradict(name,*args,**kwargs):
                    result=run(name,*args,**kwargs)
                    if name==COMMAND:
                        peer._record("show ap database long","success",[dict(row,name="RENAMED")])
                    return result
                with patch.object(self.session,"run",side_effect=contradict):
                    self.assertEqual(w.tick().status,"withheld")
                self.assertEqual(len(self.port_calls()),1)
                self.assertFalse(w.protection_frame()["ap_port_evidence"]["rows"])
            finally:engine.close()
