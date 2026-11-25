import tempfile,unittest
from pathlib import Path
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch
from awqm.scale_validation import scale_fixtures,scale_test,soak
from awqm.aos8 import radio_summary,associations,inventory,active_details
from awqm.client import bss_table

class ScaleValidationTests(unittest.TestCase):
    def test_full_fixture_shape(self):
        f=scale_fixtures(4)
        self.assertEqual(len(inventory(f["show ap database long"])),500)
        self.assertEqual(len(active_details(f["show ap active details"])),500)
        self.assertEqual(len(radio_summary(f["show ap radio-summary"])),1000)
        self.assertEqual(len(associations(f["show ap association"])),5000)
        self.assertEqual(len(bss_table(f["show ap bss-table"])),1000)
    def test_small_actual_engine_capacity(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/"validation"
            result=scale_test(p,aps=3,clients=4)
            self.assertEqual((result["radio_count"],result["client_count"]),(24,48))
            self.assertEqual(result["result"],"pass")
            self.assertTrue((p/"capacity-result.json").is_file())
            self.assertFalse(result["soak_validated"])
            with self.assertRaises(FileExistsError):scale_test(p,aps=3,clients=4)
    def test_shortcut_not_qualifying(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):soak(Path(root)/"soak",hours=1)

    def test_disqualifying_sample_is_terminal_and_records_measured_counts(self):
        clock = [0]
        def advance(_):
            clock[0] += 60
            if clock[0] > 180:
                self.fail("Soak continued after coverage was lost")
        with tempfile.TemporaryDirectory() as root, ExitStack() as mocks:
            def stub(target, **kwargs):
                return mocks.enter_context(patch("awqm.scale_validation." + target, **kwargs))
            stub("win11", return_value=True)
            stub("shutil.disk_usage", return_value=SimpleNamespace(free=110_000_000_000))
            stub("Store")
            stub("workers")
            engine = stub("Engine").return_value
            engine.error = None
            engine.pulse.return_value = dict(mapped_radios=4000, active_clients=15000, coherent_controllers=4)
            stub("Maintenance")
            stub("BackgroundMaintenance").return_value.tick.return_value = {"status":"ok"}
            stub("DiagnosticView").return_value.page.return_value = {"total":4000}
            stub("time", monotonic=lambda:clock[0], time=lambda:clock[0],
                 perf_counter=lambda:clock[0], sleep=advance)
            result = soak(Path(root)/"soak")
            self.assertEqual((result["state"], result["checks"], result["missed_checks"]), ("failed", 1, 1))
            self.assertEqual(result["failure"], "coverage_mismatch")
            self.assertEqual(result["last_sample"]["active_clients"], 15000)
            self.assertEqual(result["elapsed_seconds"], 180)
            engine.close.assert_called_once()

    def run_terminal_scenario(self, *, final_step=60.25, skip_last=False, drain_seconds=0,
                              cleanup_error=None, runtime_interrupt=None, maintenance_status="ok"):
        duration = 72 * 3600
        clock = [0.0]
        records = []
        coverage = dict(mapped_radios=4000, active_clients=20000, coherent_controllers=4)
        def advance(_):
            if runtime_interrupt and clock[0] >= 180:
                raise runtime_interrupt
            if skip_last and clock[0] == duration - 120:
                clock[0] = duration
            elif clock[0] == duration - 60:
                clock[0] += final_step
            else:
                clock[0] += 60
        def finish():
            clock[0] += drain_seconds
            return {"status":maintenance_status}
        with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
            def stub(name, **options):
                return stack.enter_context(patch("awqm.scale_validation." + name, **options))
            stub("win11", return_value=True)
            stub("shutil.disk_usage", return_value=SimpleNamespace(free=110_000_000_000))
            stub("Store")
            stub("workers")
            engine = stub("Engine").return_value
            engine.error = None
            engine.pulse = lambda: coverage
            engine.published_radios = ()
            engine.close.side_effect = cleanup_error
            stub("Maintenance")
            maintenance = stub("BackgroundMaintenance").return_value
            maintenance.tick = lambda: {"status":"ok"}
            maintenance.finish = finish
            view = stub("DiagnosticView").return_value
            view.publish = lambda *args: None
            view.page = lambda: {"total":4000}
            stub("time", monotonic=lambda:clock[0], time=lambda:clock[0],
                 perf_counter=lambda:clock[0], sleep=advance)
            def capture(path, report):
                if report["state"] != "running":
                    records.append(report.copy())
            stub("write_json", new=capture)
            if cleanup_error or runtime_interrupt:
                with self.assertRaises(type(cleanup_error or runtime_interrupt)):
                    soak(Path(root)/"soak")
            else:
                soak(Path(root)/"soak")
            engine.close.assert_called_once()
            maintenance.close.assert_called_once()
        return [(r["state"],r["failure"]) for r in records]

    def test_soak_records_success_only_after_clean_shutdown(self):
        self.assertEqual(self.run_terminal_scenario(), [("passed",None)])
        self.assertEqual(self.run_terminal_scenario(cleanup_error=RuntimeError("cleanup")),
                         [("failed","cleanup_failure")])

    def test_soak_rejects_gap_at_the_boundary_and_in_final_drain(self):
        for args, failure in (({"final_step":125},"clock_or_execution_gap"),
                              ({"skip_last":True},"missed_validation_interval"),
                              ({"drain_seconds":121},"clock_or_execution_gap")):
            with self.subTest(args=args):
                self.assertEqual(self.run_terminal_scenario(**args), [("failed",failure)])

    def test_soak_preserves_first_failure_and_operator_interrupt_on_shutdown(self):
        self.assertEqual(self.run_terminal_scenario(maintenance_status="failed",cleanup_error=RuntimeError("cleanup")),
                         [("failed","maintenance_or_disk_failure")])
        self.assertEqual(self.run_terminal_scenario(runtime_interrupt=KeyboardInterrupt()),
                         [("incomplete","interrupted")])
