import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest.mock import patch

import paramiko

from awqm import device_profiles as devices
from awqm.collector import HostKeyUnapproved
from awqm.ssh_validation import CPU, PASSWORD, USER, known_file, server


class ProfileTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = devices.Profiles(self.root)
        self.profile = devices.DeviceProfile("WLC-1", "192.0.2.1", USER)

    def test_first_run_and_transient_password_lifetime(self):
        self.assertEqual(self.store.list(), [])
        self.assertEqual(list(self.root.iterdir()), [])
        self.store.save(self.profile, PASSWORD, False)
        self.assertEqual(self.store.password(self.profile), PASSWORD)
        self.assertFalse(self.store.remembered(self.profile))
        reopened = devices.Profiles(self.root)
        self.assertEqual(reopened.list(), [self.profile])
        self.assertEqual(reopened.password(self.profile), "")
        for file in self.root.rglob("*"):
            if file.is_file():
                self.assertNotIn(PASSWORD.encode(), file.read_bytes())

    def test_edit_and_remove_invalidate_even_another_windows_stale_password(self):
        self.store.save(self.profile, PASSWORD, False)
        second = devices.Profiles(self.root)
        renamed = replace(self.profile, host="192.0.2.2")
        second.save(renamed, "different-password", False)
        self.assertEqual(self.store.password(self.profile), "")
        self.assertEqual(self.store.password(renamed), "")
        second.remove(renamed.id)
        second.save(self.profile, "", False)
        self.assertEqual(self.store.password(self.profile), "")

    @unittest.skipUnless(sys.platform == "win32", "current-user Windows DPAPI")
    def test_dpapi_saved_password_reopens_and_opt_out_removes_it(self):
        self.store.save(self.profile, PASSWORD, True)
        reopened = devices.Profiles(self.root)
        self.assertTrue(reopened.remembered(self.profile))
        self.assertEqual(reopened.password(self.profile), PASSWORD)
        self.assertEqual(reopened.password(replace(self.profile, username="other")), "")
        for file in self.root.rglob("*"):
            if file.is_file():
                self.assertNotIn(PASSWORD.encode(), file.read_bytes())
        reopened.save(self.profile, "just-this-session", False)
        self.assertEqual(reopened.password(self.profile), "just-this-session")
        self.assertFalse(self.store.remembered(self.profile))
        self.assertEqual(self.store.password(self.profile), "")
        reopened.save(self.profile, PASSWORD, True)
        reopened.remove(self.profile.id)
        self.assertEqual(self.store.list(), [])
        self.assertEqual(reopened.password(self.profile), "")

    def test_bad_profile_and_password_inputs(self):
        for field, value in (("host", "controller.invalid"), ("host", "192.0.2.1:22"),
                             ("host", "0.0.0.0"), ("host", "224.0.0.1"), ("host", "::%bad"),
                             ("username", "user\nshow version"), ("id", "../../profile"),
                             ("fingerprint", "SHA256:abc"), ("known_hosts", "relative/file")):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.store.save(replace(self.profile, **{field: value}), PASSWORD, False)
        with self.assertRaises(ValueError):
            self.store.save(self.profile, "", True)
        self.assertEqual(self.store.list(), [])

    def test_concurrent_writers_cannot_exceed_four_or_partially_replace_a_profile(self):
        profiles = [replace(self.profile, id=f"WLC-{i}", host=f"192.0.2.{i}") for i in range(1, 6)]

        def add(profile):
            try:
                devices.Profiles(self.root).save(profile, "", False)
                return True
            except ValueError:
                return False

        with ThreadPoolExecutor(max_workers=5) as pool:
            self.assertEqual(sum(pool.map(add, profiles)), 4)
        saved = self.store.list()
        self.assertEqual(len(saved), 4)
        with self.assertRaises(ValueError):
            self.store.save(replace(saved[1], host=saved[0].host), PASSWORD, False)
        self.assertEqual(self.store.list(), saved)


class DeviceSSHTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.trusted = paramiko.RSAKey.generate(2048)
        cls.changed = paramiko.RSAKey.generate(2048)

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        self.ssh_dir = self.home / ".ssh"
        self.ssh_dir.mkdir()
        home = patch.object(Path, "home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)
        self.profile = devices.DeviceProfile("WLC-1", "127.0.0.1", USER)

    def test_trust_sources_open_allowlisted_shell_on_same_authenticated_connection(self):
        for source in ("pin", "default", "selected"):
            with self.subTest(source=source), server(self.trusted) as (port, peer, state):
                profile = self.profile
                if source == "pin":
                    profile = replace(profile, fingerprint=self.trusted.fingerprint)
                elif source == "default":
                    known_file(self.ssh_dir, port, self.trusted)
                else:
                    profile = replace(profile, known_hosts=str(known_file(self.home, port, self.trusted)))
                session = devices.open_session(profile, PASSWORD, port=port)
                try:
                    self.assertEqual(session.run("show cpuload"), CPU)
                    self.assertEqual(peer.attempts, [USER])
                    self.assertEqual(peer.commands, ["show cpuload"])
                    with self.assertRaises(ValueError):
                        session.run("show cpuload\nconfigure terminal")
                finally:
                    session.close()

    def test_key_change_blocks_password_for_file_pin_and_conflicting_file_plus_pin(self):
        for source in ("file", "pin", "combined"):
            with self.subTest(source=source), server(self.trusted) as (port, peer, state):
                if source == "pin":
                    profile = replace(self.profile, fingerprint=self.changed.fingerprint)
                else:
                    key = self.trusted if source == "combined" else self.changed
                    path = known_file(self.home, port, key)
                    profile = replace(self.profile, known_hosts=str(path),
                                      fingerprint=self.changed.fingerprint if source == "combined" else "")
                with self.assertRaises(devices.HostKeyMismatch):
                    devices.open_session(profile, PASSWORD, port=port)
                self.assertEqual(peer.attempts, [])

    def test_no_trust_or_missing_selected_file_blocks_connection(self):
        for profile in (self.profile, replace(self.profile, fingerprint=self.trusted.fingerprint,
                         known_hosts=str(self.home / "missing"))):
            with self.assertRaises(HostKeyUnapproved):
                devices.open_session(profile, PASSWORD, port=1)

    def test_existing_openssh_file_can_include_unrelated_ca_and_revoked_entries(self):
        with server(self.trusted) as (port, peer, state):
            trusted_line = f"{self.trusted.get_name()} {self.trusted.get_base64()}\n"
            path = self.ssh_dir / "known_hosts"
            hashed = paramiko.HostKeys.hash_host(f"[127.0.0.1]:{port}")
            path.write_text("@cert-authority *.invalid " + trusted_line
                            + "@revoked 192.0.2.1 " + trusted_line
                            + hashed + " " + trusted_line, encoding="ascii")
            session = devices.open_session(self.profile, PASSWORD, port=port)
            try:
                self.assertEqual(session.run("show cpuload"), CPU)
            finally:
                session.close()

    def test_revoked_key_cannot_be_resurrected_by_explicit_pin(self):
        with server(self.trusted) as (port, peer, state):
            path = known_file(self.ssh_dir, port, self.trusted)
            with path.open("a", encoding="ascii") as stream:
                stream.write(f"@revoked * {self.trusted.get_name()} {self.trusted.get_base64()}\n")
            with self.assertRaises(devices.HostKeyMismatch):
                devices.open_session(replace(self.profile, fingerprint=self.trusted.fingerprint),
                                     PASSWORD, port=port)
            self.assertEqual(peer.attempts, [])

    def test_rejected_password_is_attempted_once_and_never_opens_shell(self):
        with server(self.trusted) as (port, peer, state):
            profile = replace(self.profile, fingerprint=self.trusted.fingerprint)
            with self.assertRaises(paramiko.AuthenticationException):
                devices.open_session(profile, "wrong-password", port=port)
            self.assertEqual(peer.attempts, [USER])
            self.assertEqual(peer.shell_requests, 0)

    def test_authenticated_but_no_pty_has_distinct_failure(self):
        with patch("awqm.ssh_validation.FixtureInterface.check_channel_pty_request", return_value=False):
            with server(self.trusted) as (port, peer, state):
                with self.assertRaises(devices.DeviceShellUnavailable):
                    devices.open_session(replace(self.profile, fingerprint=self.trusted.fingerprint),
                                         PASSWORD, port=port)
                self.assertEqual(peer.attempts, [USER])
                self.assertEqual(peer.shell_requests, 0)

    def test_stop_while_waiting_for_handshake_or_pty_closes_socket_before_peer_resumes(self):
        from awqm.ssh_validation import FixtureInterface
        for target, method, expected_logins in ((paramiko.Transport, "get_server_key", []),
                                                 (FixtureInterface, "check_channel_pty_request", [USER])):
            with self.subTest(stage=method):
                waiting, release, stopped = Event(), Event(), Event()
                original = getattr(target, method)
                def delayed(*args, **kwargs):
                    waiting.set()
                    release.wait(5)
                    return original(*args, **kwargs)
                profile = replace(self.profile, fingerprint=self.trusted.fingerprint)
                with patch.object(target, method, delayed), server(self.trusted) as (port, peer, state):
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        attempt = pool.submit(devices.open_session, profile, PASSWORD, port=port, stopped=stopped)
                        try:
                            self.assertTrue(waiting.wait(2))
                            stopped.set()
                            with self.assertRaises(ConnectionAbortedError):
                                attempt.result(timeout=2)
                        finally:
                            release.set()
                    self.assertEqual(peer.attempts, expected_logins)
                    self.assertEqual(peer.shell_requests, 0)
