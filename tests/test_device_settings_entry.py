import json
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from awqm.desktop_app import Dashboard
from awqm.device_web import DeviceWeb
from awqm.device_profiles import Profiles


class DeviceSettingsEntryTests(unittest.TestCase):
    def test_browser_capability_never_exposes_password_or_token(self):
        opener = build_opener(ProxyHandler({}))
        with TemporaryDirectory() as root:
            with patch("awqm.device_web.Profiles", return_value=Profiles(root)):
                with Dashboard() as desktop:
                    base = desktop.select(None)
                    url = urlsplit(desktop.browser_url(desktop.local_devices()))
                    token = url.fragment.removeprefix("settings=")
                    self.assertGreaterEqual(len(token), 32)
                    self.assertEqual(url.path, "/devices")
                    self.assertEqual(desktop.browser_url("https://awqm.example/"), "https://awqm.example/")
                    with opener.open(base + "devices") as page:
                        self.assertIn("장비 및 계정 설정", page.read().decode())
                    with opener.open(base + "api/snapshot") as response:
                        self.assertNotIn(token, response.read().decode())
                    def request(origin, key, data):
                        req = Request(base + "api/desktop/devices", data=json.dumps(data).encode(), method="POST",
                                      headers={"Origin": origin, "X-AWQM-Settings": key, "Content-Type": "application/json"})
                        return opener.open(req, timeout=3)
                    read = {"action":"read","id":"WLC-1","table":"AP","page":0}
                    for origin, key in ((base[:-1], "wrong"), ("http://outside.invalid", token)):
                        with self.assertRaises(HTTPError) as result:
                            request(origin, key, read)
                        self.assertEqual(result.exception.code, 403)
                        result.exception.close()
                    with request(base[:-1], token, read) as response:
                        body = response.read().decode()
                    self.assertNotIn(token, body)
                    self.assertEqual(len(json.loads(body)["slots"]), 4)
                    self.assertEqual(json.loads(body)["local_ownership"], "idle")
                    self.assertNotIn('"password":', body)
                    handoff = {"action":"prepare_handoff"}
                    for origin, key in ((base[:-1], "wrong"), ("http://outside.invalid", token)):
                        with self.assertRaises(HTTPError) as result:
                            request(origin, key, handoff)
                        self.assertEqual(result.exception.code, 403)
                        result.exception.close()
                    with request(base[:-1], token, handoff) as response:
                        self.assertTrue(json.load(response)["handoff_pending"])
                    with request(base[:-1], token, read) as response:
                        self.assertTrue(json.load(response)["handoff_pending"])
                    with request(base[:-1], token, {"action":"enable_local"}) as response:
                        self.assertFalse(json.load(response)["handoff_pending"])
                    search = {"action":"search","table":"AP","query":"test","page":0}
                    for origin, key in ((base[:-1], "wrong"), ("http://outside.invalid", token)):
                        with self.assertRaises(HTTPError) as result:
                            request(origin, key, search)
                        self.assertEqual(result.exception.code, 403)
                        result.exception.close()
                    with request(base[:-1], token, search) as response:
                        found = json.load(response)
                    self.assertEqual(found["table"]["scope"], "all")
                    self.assertEqual(found["table"]["total"], 0)
                    with request(base[:-1], token, {"action":"interval","seconds":120}) as response:
                        self.assertEqual(json.load(response), {"ok":True})
                    self.assertEqual(Profiles(root).poll_interval(), 120)
                    from awqm.collection_owner import CollectionBusy
                    with patch.object(desktop.devices, "dispatch", side_effect=CollectionBusy("private detail")):
                        with self.assertRaises(HTTPError) as result:
                            request(base[:-1], token, read)
                        self.assertEqual(result.exception.code, 409)
                        error = result.exception.read().decode()
                        self.assertNotIn("private detail", error)
                        self.assertEqual(json.loads(error)["reason"], "unknown")
                        result.exception.close()
                    with self.assertRaises(HTTPError) as result:
                        request(base[:-1], token, {"action":"interval","seconds":1})
                    self.assertEqual(result.exception.code, 400)
                    result.exception.close()
