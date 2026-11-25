import json
import socket
import sys
import unittest
from contextlib import ExitStack
from urllib.request import ProxyHandler, build_opener
from unittest.mock import patch

from awqm.desktop_endpoint import POLICY_KEY, SERVICE_KEY, approved_endpoint, dashboard_url
from awqm.desktop_app import Dashboard, desktop_smoke, verify_manager


class DesktopStartupTests(unittest.TestCase):
    def test_user_writable_manager_tree_cannot_be_launched_for_elevation(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            directory = Path(tmp) / "Program Files" / "AWQM"
            directory.mkdir(parents=True)
            app = directory / "AWQM.exe"
            app.write_bytes(b"test executable")
            dll = directory / "library.dll"
            dll.write_bytes(b"test runtime")
            def check(path, *, trust_current_owner):
                self.assertFalse(trust_current_owner)
                if path == dll:
                    raise ValueError("untrusted DLL ACL")
            with patch("awqm.desktop_app.installed_program", return_value=app), patch("awqm.windows_security.check_program_acl", side_effect=check):
                with self.assertRaisesRegex(ValueError, "untrusted DLL"):
                    verify_manager(app)

    @unittest.skipUnless(sys.platform == "win32", "Windows machine settings")
    def test_site_policy_takes_precedence_on_an_operator_pc(self):
        with patch("awqm.desktop_endpoint.registry_endpoint", return_value="https://operations.example/") as read:
            self.assertEqual(approved_endpoint("missing"), "https://operations.example/")
        read.assert_called_once_with(POLICY_KEY)

    @unittest.skipUnless(sys.platform == "win32", "Windows machine settings")
    def test_local_endpoint_only_opens_for_a_running_service(self):
        def registry(path):
            return "https://local.example:8443/" if path == SERVICE_KEY else None
        with patch("awqm.desktop_endpoint.registry_endpoint", side_effect=registry):
            self.assertIsNone(approved_endpoint("stopped"))
            self.assertEqual(approved_endpoint("running"), "https://local.example:8443/")
        with patch("awqm.desktop_endpoint.registry_endpoint", side_effect=ValueError):
            self.assertIsNone(approved_endpoint("running"))

    def test_only_an_https_origin_is_accepted(self):
        self.assertEqual(dashboard_url("https://Awqm.Example:8443/"), "https://awqm.example:8443/")
        self.assertEqual(dashboard_url("https://awqm.example"), "https://awqm.example/")
        for value in ("http://host", "https://name@host", "https://host?x", "https://host/#x",
                      "https://host/other", "https://host:0/", "https://bad\\host/",
                      "https://host/\n", "//host", "https://host:99999", "https://host/?"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                dashboard_url(value)

    def test_desktop_demo_owns_its_port_and_is_closed(self):
        with ExitStack() as resources:
            blocker = resources.enter_context(socket.socket())
            blocker.bind(("127.0.0.1", 0))
            blocker.listen()
            first = resources.enter_context(Dashboard())
            other = resources.enter_context(Dashboard())
            url = first.select(None)
            self.assertEqual(first.select(None), url)
            self.assertNotEqual(other.select(None), url)
            self.assertNotEqual(blocker.getsockname()[1], first.demo_port)
            opener = build_opener(ProxyHandler({}))
            with opener.open(url + "api/snapshot", timeout=3) as response:
                self.assertGreater(len(json.load(response)["radios"]), 0)
            self.assertEqual(first.select("https://awqm.example:8443/"), "https://awqm.example:8443/")
            port = first.demo_port
        with socket.socket() as check:
            self.assertNotEqual(check.connect_ex(("127.0.0.1", port)), 0)

    @unittest.skipUnless(sys.platform == "win32", "Windows controls")
    def test_real_hidden_desktop_never_opens_a_browser_or_manages_services(self):
        with patch("webbrowser.open", side_effect=AssertionError), patch("awqm.manager.control", side_effect=AssertionError):
            result = desktop_smoke()
        self.assertTrue(result["native_controls"] and result["loopback_dashboard"])
        self.assertGreater(result["radios"], 0)
