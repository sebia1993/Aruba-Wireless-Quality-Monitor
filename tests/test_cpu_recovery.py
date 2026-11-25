import unittest

from awqm.fixture_session import FixtureSession
from awqm.scheduler import Poll, Protection
from awqm.worker import ControllerWorker
from test_worker import setup


class CpuRecoveryTests(unittest.TestCase):
    def test_unreadable_poll_keeps_critical_worker_and_detail_job_protected(self):
        now = [0]
        session = FixtureSession(1)
        worker = ControllerWorker("WLC-1", lambda: session, clock=lambda: now[0],
                                  wall=lambda: now[0], mode="standard")
        self.addCleanup(worker.close)

        def poll_at(at, response):
            now[0] = at
            session.responses["show cpuload"] = response
            result = worker.tick()
            self.assertEqual(result.command, "show cpuload")
            for _ in range(10):
                if worker.tick() is None:
                    break
            return result.status

        low = "user 10%, system 10%, idle 80%"
        self.assertEqual(poll_at(0, low), "success")
        self.assertEqual(poll_at(30, "user 80%, system 10%, idle 10%"), "success")
        job = worker.submit_detail("critical-window", "show ap debug client-table", session.ap)
        self.assertEqual(poll_at(60, "incomplete CPU output"), "parse_error")
        frame = worker.protection_frame()
        self.assertIsNone(frame["cpu"])
        self.assertEqual(frame["protection"], "unknown")
        self.assertEqual(worker.latest["show ap radio-summary"].observed_at, 0)

        for at in (90, 120):
            self.assertEqual(poll_at(at, low), "success")
            self.assertEqual(worker.protection_frame()["protection"], "critical")
            self.assertEqual(worker.jobs.get(job.job_id).reason, "controller_protection")
            self.assertEqual(session.round, 0)
        self.assertEqual(poll_at(150, low), "success")
        self.assertEqual(worker.protection_frame()["protection"], "normal")
        self.assertEqual(worker.jobs.get(job.job_id).status, "completed")
        self.assertEqual(session.round, 1)

    def test_first_malformed_cpu_after_reconnect_does_not_resume_heavy_polling(self):
        clock, session, worker = setup()
        self.addCleanup(worker.close)
        cpu = "show cpuload"
        session.responses[cpu] = "user 80%, system 10%, idle 10%"
        self.assertEqual(worker.tick().status, "success")
        clock.t = 30
        session.responses[cpu] = TimeoutError()
        self.assertEqual(worker.tick().status, "transport_error")
        clock.t = worker.next_connect
        session.responses[cpu] = "incomplete CPU output"
        self.assertEqual(worker.tick().status, "parse_error")
        self.assertIsNone(worker.tick())
        self.assertEqual(session.calls, [cpu, cpu, cpu])

        session.responses[cpu] = "user 10%, system 10%, idle 80%"
        for expected in ("critical", "critical", "normal"):
            clock.t += 30
            self.assertEqual(worker.tick().command, cpu)
            self.assertEqual(worker.protection_frame()["protection"], expected)

    def test_recovery_samples_must_be_fresh_and_distinct(self):
        detail = Poll("detail", 60, 10, 3, True)
        for readings, expected in (((90,), "critical"), ((75, 75), "limited")):
            with self.subTest(latch=expected):
                protection = Protection()
                for at, value in enumerate(readings):
                    protection.cpu(value, at)
                protection.cpu(20, 30)
                protection.cpu(20, 60)
                protection.cpu(20, 180)  # earlier readings are stale
                protection.cpu(25, 180)  # same sample time must not advance recovery
                protection.cpu(10, 100)  # delayed old reading cannot move time backwards
                self.assertEqual(protection.last_at, 180)
                protection.cpu(20, 210)
                self.assertEqual(protection.effective(210), expected)
                self.assertFalse(protection.allowed(detail, 210))
                protection.cpu(20, 240)
                self.assertEqual(protection.effective(240), "normal")
                self.assertTrue(protection.allowed(detail, 240))
