import json
import sys
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from awqm.desktop_app import Dashboard, DesktopWindow


class DeviceSettingsEntryTests(unittest.TestCase):
    def test_only_desktop_browser_can_request_the_native_window(self):
        opener = build_opener(ProxyHandler({}))
        with Dashboard() as desktop:
            base = desktop.select(None)
            url = urlsplit(desktop.browser_url(base))
            token = url.fragment.removeprefix("settings=")
            self.assertGreaterEqual(len(token), 32)
            self.assertEqual(desktop.browser_url("https://awqm.example/"), "https://awqm.example/")
            with opener.open(base) as page:
                self.assertIn("실제 장비 설정", page.read().decode())
            with opener.open(base + "api/snapshot") as response:
                self.assertNotIn(token, response.read().decode())
            def activate(origin, key, data=b""):
                request = Request(base + "api/desktop/settings", data=data, method="POST",
                                  headers={"Origin": origin, "X-AWQM-Settings": key})
                return opener.open(request, timeout=3)
            for origin, key, data in ((base[:-1], "wrong", b""), ("http://outside.invalid", token, b""),
                                      (base[:-1], token, b'{"password":"never-write"}')):
                with self.assertRaises(HTTPError) as result:
                    activate(origin, key, data)
                result.exception.close()
                self.assertFalse(desktop.settings_requested.is_set())
            with activate(base[:-1], token) as response:
                self.assertEqual(json.load(response), {"opened": True})
            self.assertTrue(desktop.settings_requested.is_set())

    @unittest.skipUnless(sys.platform == "win32", "native Windows window")
    def test_registered_devices_open_native_view_and_site_policy_keeps_precedence(self):
        window = DesktopWindow.__new__(DesktopWindow)
        window.refresh = Mock()
        window.show_settings = Mock()
        window.profiles_present = True
        window.dashboard = Dashboard()
        window.url = None
        window.live = None
        with patch("webbrowser.open", side_effect=AssertionError):
            window.open()
        window.show_settings.assert_called_once()
        window.live = window.url = "https://awqm.example/"
        window.NOTICE = 4
        window.controls = {4: 123}
        with patch("webbrowser.open", return_value=True) as open_browser, patch("win32gui.SetWindowText"):
            window.open()
        open_browser.assert_called_once_with("https://awqm.example/")
