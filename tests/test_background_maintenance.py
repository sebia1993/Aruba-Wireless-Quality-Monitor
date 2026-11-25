import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from awqm.maintenance import BackgroundMaintenance


class BackgroundMaintenanceTests(unittest.TestCase):
    def test_foreground_keeps_running_and_observes_single_failure(self):
        started = threading.Event()
        finish = threading.Event()
        calls = []
        def work():
            calls.append(1)
            started.set()
            finish.wait(5)
            return {"status":"failed", "error":"RuntimeError"}
        task = SimpleNamespace(tick=work, clock=lambda:0, next_at=300,
                               stop_requested=threading.Event())
        runner = BackgroundMaintenance(task)
        try:
            with ThreadPoolExecutor(max_workers=1) as foreground:
                self.assertEqual(foreground.submit(runner.tick).result(timeout=1)["status"], "running")
                self.assertTrue(started.wait(1))
                self.assertEqual(foreground.submit(runner.tick).result(timeout=1)["status"], "running")
                finish.set()
                self.assertEqual(runner.finish()["status"], "failed")
                self.assertEqual(runner.tick()["error"], "RuntimeError")
                self.assertEqual(calls, [1])
        finally:
            finish.set()
            runner.close()
        self.assertTrue(task.stop_requested.is_set())
