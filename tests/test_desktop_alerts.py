import unittest
from tempfile import TemporaryDirectory

from awqm.device_profiles import Profiles
from awqm.device_web import DeviceWeb


class Monitor:
    def __init__(self):
        self.states = {}

    def start(self, profile, password, *, once=False):
        self.states[profile.id] = {"profile": profile, "status":"연결 중", "running":True}

    def state(self, id):
        return self.states.get(id, {})

    def stop(self, id):
        self.states[id].update(status="중지됨", running=False)

    def forget(self, id):
        self.states.pop(id, None)

    def close(self):
        pass


class DesktopAlertsTests(unittest.TestCase):
    def test_opt_in_deduplication_and_explicit_stop(self):
        with TemporaryDirectory() as root:
            profiles = Profiles(root)
            monitor = Monitor()
            web = DeviceWeb(profiles, monitor)
            data = {"action":"save","id":"WLC-1","host":"192.0.2.5","username":"test",
                    "password":"fixture","remember":False,"fingerprint":"","known_hosts":""}
            self.assertFalse(web.alerts_enabled)
            web.dispatch(data)
            monitor.states["WLC-1"].update(status="SSH 연결 실패", running=False)
            self.assertEqual(web.poll_alerts(), [])
            web.dispatch({"action":"alerts","enabled":True})
            self.assertTrue(Profiles(root).alerts_enabled())
            self.assertEqual(web.poll_alerts()[0]["status"], "SSH 연결 실패")
            self.assertEqual(web.poll_alerts(), [])
            monitor.states["WLC-1"].update(status="조회 중", running=True)
            self.assertEqual(web.poll_alerts(), [])
            monitor.states["WLC-1"].update(status="조회 완료", stale=True)
            self.assertEqual(web.poll_alerts()[0]["status"], "관측 갱신 지연")
            web.dispatch({"action":"stop","id":"WLC-1"})
            monitor.states["WLC-1"].update(status="인증 실패")
            self.assertEqual(web.poll_alerts(), [])
            web.dispatch({**data,"action":"test"})
            monitor.states["WLC-1"].update(status="인증 실패",running=False)
            self.assertEqual(web.poll_alerts(), [])
            read = web.dispatch({"action":"read","id":"WLC-1","table":"AP","page":0})
            self.assertEqual(len(read["alerts"]),2)
            self.assertNotIn("fixture",str(read))
            with self.assertRaises(ValueError):
                web.dispatch({"action":"alerts","enabled":1})
            web.dispatch({"action":"alerts","enabled":False})
            self.assertFalse(Profiles(root).alerts_enabled())
            web.close()
