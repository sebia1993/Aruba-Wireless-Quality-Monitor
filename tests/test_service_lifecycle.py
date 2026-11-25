import json
import sys
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from awqm import service_runtime, winservice


class PollStop:
    def __init__(self, cycles):
        self.cycles = cycles

    def is_set(self):
        return self.cycles == 0

    def wait(self, timeout):
        self.cycles -= 1

    def set(self):
        self.cycles = 0


class RuntimeTests(unittest.TestCase):
    def test_runtime_failure_closes_workers_and_records_failure_without_exception_text(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            engine = Mock()
            engine.store.root = Path(directory)
            engine.pulse.side_effect = RuntimeError("private fixture")
            stop = PollStop(1)
            with self.assertRaisesRegex(RuntimeError, "private fixture"):
                service_runtime.pump(engine, Mock(), stop, path,
                                     usage=lambda _: SimpleNamespace(free=80e9, total=100e9))
            self.assertTrue(stop.is_set())
            engine.close.assert_called_once()
            self.assertEqual(json.loads(path.read_text())["state"], "failed")
            self.assertNotIn("private fixture", path.read_text())

    def test_failed_shutdown_publication_still_closes_workers(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            engine, view = Mock(), Mock()
            view.publish.side_effect = ValueError("fixture shutdown publication")
            with self.assertRaises(ValueError):
                service_runtime.pump(engine, Mock(), PollStop(0), path, view=view)
            engine.close.assert_called_once()
            self.assertEqual(json.loads(path.read_text())["state"], "failed")

    def test_wall_clock_rollback_does_not_postpone_disk_protection_or_status(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            engine = Mock()
            engine.store.root = Path(directory)
            engine.error = None
            engine.pulse.return_value = {"paused": False}
            usage = Mock(side_effect=[SimpleNamespace(free=80e9, total=100e9),
                                      SimpleNamespace(free=1e9, total=100e9)])
            status = []
            with patch.object(service_runtime, "write_json", side_effect=lambda _, record: status.append(record)):
                service_runtime.pump(engine, Mock(), PollStop(2), path, usage=usage,
                                     wall=Mock(side_effect=[2000, 1000, 1000]),
                                     clock=Mock(side_effect=[0, 5]))
            self.assertEqual([r["disk_level"] for r in status if r["state"] == "running"], ["normal", "critical"])
            self.assertEqual(status[1]["at"], 1000)
            engine.pulse.assert_called_once()


class ConfiguredRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        for name in ("data", "backup", "logs", "config", "secrets"):
            (self.root / name).mkdir()
        self.status = self.root / "logs/service-status.json"
        target = SimpleNamespace(id="fixture", ip="192.0.2.3", username="fixture",
                                 credential=self.root / "secrets/fixture.dpapi")
        self.cfg = SimpleNamespace(root=self.root, account="fixture", site="fixture",
                                   known_hosts=self.root / "secrets/known_hosts", targets=(target,))
        self.enterContext(patch.object(service_runtime, "load_config", return_value=self.cfg))
        self.identity = self.enterContext(patch.object(service_runtime, "check_identity"))
        self.layout = self.enterContext(patch.object(service_runtime, "check_layout"))
        self.exclusive = self.enterContext(patch.object(service_runtime, "exclusive", return_value=nullcontext()))
        self.collection = self.enterContext(patch.object(service_runtime, "collection_lease", return_value=nullcontext()))
        self.enterContext(patch("paramiko.HostKeys"))
        self.enterContext(patch.object(service_runtime, "secret"))

    def test_rejected_web_preflight_replaces_stale_heartbeat_only_after_exclusive_access(self):
        self.status.write_text('{"state":"running"}')
        (self.root / "config/web.json").write_text('{"unexpected":true}')
        self.exclusive.side_effect = RuntimeError("already running")
        with self.assertRaisesRegex(RuntimeError, "already running"):
            service_runtime.run_service(self.root, Event())
        self.assertEqual(json.loads(self.status.read_text())["state"], "running")
        self.exclusive.side_effect = None
        with self.assertRaises(ValueError):
            service_runtime.run_service(self.root, Event())
        self.assertEqual(json.loads(self.status.read_text())["state"], "failed")

    def test_collection_conflict_blocks_service_before_credentials_and_status(self):
        from awqm.collection_owner import CollectionBusy
        self.status.write_text('{"state":"stopped"}')
        self.collection.side_effect = CollectionBusy("owner active")
        ready, stop = Mock(), Event()
        with patch.object(service_runtime, "secret") as secret, patch.object(service_runtime, "Engine") as engine:
            with self.assertRaises(CollectionBusy):
                service_runtime.run_service(self.root, stop, ready)
        secret.assert_not_called()
        engine.assert_not_called()
        ready.assert_not_called()
        self.assertEqual(json.loads(self.status.read_text())["state"], "stopped")

    def test_failed_setup_closes_constructed_engine_and_rejects_readiness(self):
        engine, ready, stop = Mock(), Mock(), Event()
        with (patch.object(service_runtime, "Engine", return_value=engine),
              patch.object(service_runtime, "Maintenance", side_effect=ValueError("bad maintenance"))):
            with self.assertRaisesRegex(ValueError, "bad maintenance"):
                service_runtime.run_service(self.root, stop, ready)
        self.identity.assert_called_once_with("fixture")
        self.layout.assert_called_once_with(self.cfg)
        ready.assert_not_called()
        self.assertTrue(stop.is_set())
        engine.close.assert_called_once()
        self.assertEqual(json.loads(self.status.read_text())["state"], "failed")

    def test_ready_callback_can_stop_fully_initialized_runtime_before_polling(self):
        stop = Event()
        def ready():
            self.assertTrue((self.root / "data/core.db").is_file())
            stop.set()
        with patch.object(service_runtime.ConnectionGate, "connect") as connect:
            service_runtime.run_service(self.root, stop, ready)
        connect.assert_not_called()
        self.assertEqual(json.loads(self.status.read_text())["state"], "stopped")


@unittest.skipUnless(sys.platform == "win32", "requires pywin32 service framework")
class ServiceHostTests(unittest.TestCase):
    def setUp(self):
        import win32service
        import win32serviceutil
        self.scm = win32service
        # Run the real pywin32 service entry point without registering an SCM handle.
        with patch.object(win32serviceutil.ServiceFramework, "__init__", return_value=None):
            self.host = winservice.service_class()([])
        self.host.ReportServiceStatus = Mock()
        self.logged = self.enterContext(patch("servicemanager.LogErrorMsg"))

    def states(self):
        return [args[0] for args, _ in self.host.ReportServiceStatus.call_args_list]

    def test_failed_preflight_never_reports_running(self):
        with patch.object(winservice, "run_service", side_effect=ValueError("private fixture")):
            with self.assertRaisesRegex(RuntimeError, "AWQM diagnostic service failed"):
                self.host.SvcRun()
        self.assertNotIn(self.scm.SERVICE_RUNNING, self.states())
        self.assertIn(self.scm.SERVICE_START_PENDING, self.states())
        self.assertNotIn("private fixture", str(self.logged.call_args))

    def test_interrogate_does_not_replace_pending_stop_with_running(self):
        self.host.SvcStop()
        self.host.ServiceCtrlHandlerEx(self.scm.SERVICE_CONTROL_INTERROGATE, 0, None)
        self.assertEqual(self.states()[-1], self.scm.SERVICE_STOP_PENDING)

    def test_stop_after_ready_and_stop_during_startup(self):
        for stop_during_startup in (False, True):
            with self.subTest(stop_during_startup=stop_during_startup):
                self.host.ReportServiceStatus.reset_mock()
                self.host.stop_event.clear()
                def run(root, stop, on_ready):
                    self.assertEqual(self.states(), [self.scm.SERVICE_START_PENDING])
                    if stop_during_startup:
                        self.host.SvcShutdown()
                    on_ready()
                    if not stop_during_startup:
                        self.host.SvcStop()
                    self.assertTrue(stop.is_set())
                with patch.object(winservice, "run_service", side_effect=run):
                    self.host.SvcRun()
                self.assertEqual(self.scm.SERVICE_RUNNING in self.states(), not stop_during_startup)
                self.assertEqual(self.states()[-1], self.scm.SERVICE_STOP_PENDING)
