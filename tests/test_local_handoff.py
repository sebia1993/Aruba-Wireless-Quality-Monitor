"""Exercise real monitor drain and local web handoff without contacting WLCs."""
from contextlib import contextmanager
from tempfile import TemporaryDirectory
from threading import Event, Lock
from time import monotonic, sleep
import unittest

from awqm.collection_owner import CollectionBusy
from awqm.device_monitor import DeviceMonitor
from awqm.device_profiles import Profiles
from awqm.device_web import DeviceWeb
from awqm.fixture_session import FixtureSession


def until(check):
    deadline = monotonic() + 5
    while not check():
        if monotonic() >= deadline:
            raise AssertionError("fixture did not drain")
        sleep(.01)


def request(number, action="save"):
    return {"action":action, "id":f"WLC-{number}", "host":f"192.0.2.{number}",
            "username":"fixture", "password":"fixture-only", "remember":False,
            "fingerprint":"", "known_hosts":""}


class LocalHandoffTests(unittest.TestCase):
    def test_multi_slot_drain_blocks_stale_tabs_and_no_automatic_login(self):
        with TemporaryDirectory() as root:
            lock = Lock()
            closing = Event()
            release = Event()
            calls = []
            @contextmanager
            def lease():
                if not lock.acquire(blocking=False):
                    raise CollectionBusy(reason="owner_active")
                try:
                    yield
                finally:
                    lock.release()

            class SlowClose(FixtureSession):
                def close(self):
                    closing.set()
                    release.wait(5)
                    super().close()

            def factory(profile, password, *, stopped):
                calls.append(profile.id)
                return SlowClose(int(profile.id[-1]))

            monitor = DeviceMonitor(factory, owner_factory=lease, interval=600)
            resolutions = []
            def endpoint():
                resolutions.append(True)
                return "https://ops.example:8443/"
            web = DeviceWeb(Profiles(root), monitor, service_endpoint=endpoint)
            try:
                with self.assertRaises(CollectionBusy):
                    web.dispatch({"action":"service_dashboard"})
                web.dispatch(request(1))
                web.dispatch(request(2))
                until(lambda: len(calls) == 2)
                result = web.dispatch({"action":"prepare_handoff"})
                self.assertTrue(result["handoff_pending"])
                self.assertTrue(closing.wait(2))
                self.assertEqual(monitor.ownership_state(), "draining")
                self.assertTrue(lock.locked())
                with self.assertRaises(CollectionBusy):
                    web.dispatch({"action":"service_dashboard"})
                self.assertEqual(resolutions, [])
                for action in ("save", "test"):
                    with self.assertRaises(CollectionBusy) as caught:
                        web.dispatch(request(3, action))
                    self.assertEqual(caught.exception.reason, "handoff_pending")
                self.assertEqual(len(web.profiles.list()), 2)
                with self.assertRaises(CollectionBusy):
                    web.dispatch({"action":"enable_local"})
                self.assertFalse(web.monitored)
                self.assertEqual(web.poll_alerts(), [])
                # Repeated prepare is safe and doesn't release the kernel lease.
                web.dispatch({"action":"prepare_handoff"})
                self.assertTrue(lock.locked())
                release.set()
                until(lambda: monitor.ownership_state() == "idle")
                self.assertFalse(lock.locked())
                snapshot = web.dispatch({"action":"read","id":"WLC-1","table":"AP","page":0})
                self.assertTrue(snapshot["handoff_pending"])
                self.assertFalse(snapshot["slots"][0]["running"])
                self.assertEqual(snapshot["local_ownership"], "idle")
                # Handoff remains latched even after the last close.
                with self.assertRaises(CollectionBusy):
                    web.dispatch(request(3))
                self.assertEqual(web.dispatch({"action":"service_dashboard"}),
                                 {"url":"https://ops.example:8443/"})
                self.assertEqual(resolutions, [True])
                web.dispatch({"action":"enable_local"})
                with self.assertRaises(CollectionBusy):
                    web.dispatch({"action":"service_dashboard"})
                self.assertEqual(len(calls), 2)
                # Explicit enable is not a service lease, nor a grant of access.
                with lease():
                    with self.assertRaises(CollectionBusy):
                        web.dispatch(request(3, "test"))
                self.assertEqual(len(calls), 2)
            finally:
                release.set()
                web.close()
                until(lambda: monitor.ownership_state() == "idle")

    def test_close_failure_never_enables_local(self):
        with TemporaryDirectory() as root:
            class FailedClose(FixtureSession):
                def close(self):
                    raise OSError("fixture")
            monitor = DeviceMonitor(lambda *a, **k: FailedClose(1))
            web = DeviceWeb(Profiles(root), monitor)
            try:
                web.dispatch(request(1, "test"))
                until(lambda: monitor.ownership_state() == "close_unconfirmed")
                result = web.dispatch({"action":"prepare_handoff"})
                self.assertTrue(result["handoff_pending"])
                self.assertEqual(result["local_ownership"], "close_unconfirmed")
                with self.assertRaises(CollectionBusy):
                    web.dispatch({"action":"enable_local"})
                with self.assertRaises(CollectionBusy):
                    web.dispatch({"action":"service_dashboard"})
                for action in ("prepare_handoff","enable_local","service_dashboard"):
                    with self.assertRaises(ValueError):
                        web.dispatch({"action":action,"force":True})
            finally:
                web.close()

    def test_endpoint_is_fail_closed_and_not_caller_supplied(self):
        with TemporaryDirectory() as root:
            monitor = DeviceMonitor()
            web = DeviceWeb(Profiles(root), monitor)
            try:
                web.dispatch({"action":"prepare_handoff"})
                self.assertEqual(web.dispatch({"action":"service_dashboard"}), {"url":None})
                for bad in ("http://ops.example/", "https://user@ops.example/",
                            "https://ops.example/#settings=secret", "https://ops.example/path"):
                    web.service_endpoint = lambda: bad
                    self.assertEqual(web.dispatch({"action":"service_dashboard"}), {"url":None})
                def denied():
                    raise PermissionError("protected configuration")
                web.service_endpoint = denied
                self.assertEqual(web.dispatch({"action":"service_dashboard"}), {"url":None})
                with self.assertRaises(ValueError):
                    web.dispatch({"action":"service_dashboard","url":"https://ops.example/"})
                web.service_endpoint = lambda: "https://Ops.Example:8443"
                self.assertEqual(web.dispatch({"action":"service_dashboard"}),
                                 {"url":"https://ops.example:8443/"})
                self.assertEqual(web.profiles.list(), [])
            finally:
                web.close()
