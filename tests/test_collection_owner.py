"""Cooperative collection-owner, drain and HTTP/SCM regression coverage."""
from contextlib import contextmanager
import subprocess
import sys
from threading import Event, Lock
from time import monotonic, sleep
import unittest
from unittest.mock import patch
from uuid import uuid4

from awqm.collection_owner import CollectionBusy, _windows_lease, collection_lease, local_collection_lease
from awqm.device_monitor import DeviceMonitor
from awqm.device_profiles import DeviceProfile
from awqm.fixture_session import FixtureSession
from awqm.ssh_shell import ShellSession
from types import SimpleNamespace


def until(check):
    deadline = monotonic() + 4
    while not check():
        if monotonic() > deadline:
            raise AssertionError("ownership did not drain")
        sleep(.005)


class Guard:
    def __init__(self):
        self.lock = Lock()

    @contextmanager
    def lease(self):
        if not self.lock.acquire(blocking=False):
            raise CollectionBusy("busy")
        try:
            yield
        finally:
            self.lock.release()


def profile(n):
    return DeviceProfile(f"WLC-{n}", f"192.0.2.{n}", "fixture")


class OwnershipTests(unittest.TestCase):
    def test_pending_login_and_close_hold_one_lease_until_last_slot_drains(self):
        guard = Guard()
        pending, release, closing, finish = Event(), Event(), Event(), Event()
        class SlowClose(FixtureSession):
            def close(self):
                closing.set()
                finish.wait(4)
                super().close()
        def factory(p, password, *, stopped):
            if p.id == "WLC-1":
                pending.set()
                release.wait(4)
                return SlowClose(1)
            return FixtureSession(2)
        monitor = DeviceMonitor(factory, owner_factory=guard.lease)
        self.addCleanup(monitor.close)
        self.assertEqual(monitor.ownership_state(), "idle")
        try:
            monitor.start(profile(1), "fixture")
            self.assertEqual(monitor.ownership_state(), "active")
            self.assertTrue(pending.wait(2))
            monitor.start(profile(2), "fixture")
            monitor.stop("WLC-1")
            monitor.stop("WLC-2")
            # A UI stop is not an SSH drain acknowledgement.
            self.assertEqual(monitor.ownership_state(), "draining")
            with self.assertRaises(CollectionBusy), guard.lease():
                pass
            release.set()
            self.assertTrue(closing.wait(2))
            with self.assertRaises(CollectionBusy), guard.lease():
                pass
        finally:
            release.set()
            finish.set()
            monitor.close()
        until(lambda: not guard.lock.locked())
        self.assertEqual(monitor.ownership_state(), "idle")
        with guard.lease():
            pass

    def test_service_owner_rejects_local_test_before_login(self):
        guard = Guard()
        def never(*args, **kwargs):
            raise AssertionError("SSH must not start")
        monitor = DeviceMonitor(never, owner_factory=guard.lease)
        self.addCleanup(monitor.close)
        with guard.lease():
            with self.assertRaises(CollectionBusy):
                monitor.start(profile(1), "fixture", once=True)
            self.assertIsNone(monitor.state("WLC-1")["profile"])

    def test_failed_thread_creation_releases_lease(self):
        guard = Guard()
        monitor = DeviceMonitor(lambda *a, **k: None, owner_factory=guard.lease)
        self.addCleanup(monitor.close)
        with patch("awqm.device_monitor.Thread", side_effect=RuntimeError("fixture")):
            with self.assertRaises(RuntimeError):
                monitor.start(profile(1), "fixture")
        self.assertFalse(guard.lock.locked())
        self.assertIsNone(monitor.state("WLC-1")["profile"])

    def test_close_failure_retains_guard_and_blocks_new_login(self):
        guard = Guard()
        class FailedClose(FixtureSession):
            def close(self):
                raise OSError("fixture")
        monitor = DeviceMonitor(lambda *a, **k: FailedClose(1), owner_factory=guard.lease)
        self.addCleanup(monitor.close)
        monitor.start(profile(1), "fixture", once=True)
        until(lambda: monitor.state("WLC-1")["status"] == "SSH 종료 확인 실패")
        self.assertTrue(guard.lock.locked())
        self.assertEqual(monitor.ownership_state(), "close_unconfirmed")
        with self.assertRaises(CollectionBusy):
            monitor.start(profile(2), "fixture")
        # The real named object is kept until process exit. This test guard
        # is private to the test and needs no operating-system cleanup.
        self.assertFalse(monitor.state("WLC-1")["running"])

    def test_channel_close_error_still_closes_transport(self):
        closed = Event()
        def bad():
            raise OSError("fixture")
        shell = ShellSession(SimpleNamespace(close=closed.set))
        shell.channel = SimpleNamespace(close=bad)
        with self.assertRaises(OSError):
            shell.close()
        self.assertTrue(closed.is_set())
        self.assertIsNone(shell.channel)

    def test_scm_uncertainty_fails_closed(self):
        guard = Guard()
        with patch("awqm.collection_owner.collection_lease", guard.lease), patch("awqm.collection_owner.sys.platform", "win32"):
            for state in ("unknown", "running", "starting", "stopping", "other"):
                with patch("awqm.desktop_app.service_state", return_value=state):
                    with self.assertRaises(CollectionBusy), local_collection_lease():
                        pass
                self.assertFalse(guard.lock.locked())
            with patch("awqm.desktop_app.service_state", side_effect=OSError):
                with self.assertRaises(CollectionBusy), local_collection_lease():
                    pass
            for state in ("missing", "stopped"):
                with patch("awqm.desktop_app.service_state", return_value=state), local_collection_lease():
                    self.assertTrue(guard.lock.locked())

    @unittest.skipUnless(sys.platform == "win32", "requires Windows named objects")
    def test_windows_cross_process_conflict_and_release(self):
        name = "Global\\AWQM.Test." + uuid4().hex
        code = (
            "import sys\nfrom awqm.collection_owner import _windows_lease, CollectionBusy\n"
            "try:\n    with _windows_lease(sys.argv[1]): pass\n"
            "except CollectionBusy: sys.exit(17)\n"
        )
        def child():
            return subprocess.run([sys.executable, "-c", code, name], timeout=20,
                                  capture_output=True, check=False).returncode
        with _windows_lease(name):
            self.assertEqual(child(), 17)
        self.assertEqual(child(), 0)

    def test_release_error_blocks_handoff_and_new_collection(self):
        @contextmanager
        def lease():
            yield
            raise OSError("fixture release failure")
        monitor = DeviceMonitor(lambda *a, **k: FixtureSession(1), owner_factory=lease)
        self.addCleanup(monitor.close)
        monitor.start(profile(1), "fixture", once=True)
        until(lambda: monitor.ownership_state() == "close_unconfirmed")
        with self.assertRaises(CollectionBusy) as caught:
            monitor.start(profile(2), "fixture")
        self.assertEqual(caught.exception.reason, "close_unconfirmed")

    @unittest.skipUnless(sys.platform == "win32", "requires Windows named objects")
    def test_windows_repeat_release_from_other_thread_ignores_stale_last_error(self):
        import ctypes
        from threading import Thread
        name = "Global\\AWQM.Test." + uuid4().hex
        for _ in range(4):
            ctypes.set_last_error(183)
            lease = _windows_lease(name)
            lease.__enter__()
            errors = []
            try:
                with self.assertRaises(CollectionBusy) as caught, _windows_lease(name):
                    pass
                self.assertEqual(caught.exception.reason, "owner_active")
            finally:
                def release():
                    try:
                        lease.__exit__(None, None, None)
                    except Exception as exc:
                        errors.append(exc)
                thread = Thread(target=release)
                thread.start()
                thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
        with _windows_lease(name):
            pass
