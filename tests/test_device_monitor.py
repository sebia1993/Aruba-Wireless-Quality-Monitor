import threading
import time
import unittest
from types import SimpleNamespace

import paramiko

from awqm.device_monitor import DeviceMonitor
from awqm.device_profiles import DeviceProfile, DeviceShellUnavailable, HostKeyMismatch
from awqm.collector import HostKeyUnapproved
from awqm.fixture_session import FixtureSession, table
from awqm.ssh_shell import ShellSession


def profile(number=1):
    return DeviceProfile(f"WLC-{number}", f"192.0.2.{number}", "readonly")


def until(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("worker did not reach the expected state")
        time.sleep(.005)


class Session(FixtureSession):
    def __init__(self, index=1):
        super().__init__(index)
        self.calls = []

    def run(self, name, **kwargs):
        self.calls.append(name)
        return super().run(name, **kwargs)


class DeviceMonitorTests(unittest.TestCase):
    def monitor(self, factory, interval=600):
        monitor = DeviceMonitor(session_factory=factory, interval=interval)
        self.addCleanup(monitor.close)
        return monitor

    def collect_once(self, session):
        monitor = self.monitor(lambda device, password, *, stopped: session)
        monitor.start(profile(), "private", once=True)
        until(lambda: not monitor.state("WLC-1")["running"])
        self.assertTrue(session.closed)
        return monitor.state("WLC-1")

    def test_ap_inventory_includes_down_and_radio_and_bss_counts_remain_distinct(self):
        session = Session()
        session.responses["show ap database long"] = table("AP Database",
            ["Name", "Group", "AP Type", "IP Address", "Status", "Switch IP", "Standby IP", "Wired MAC Address"],
            [["DEMO-AP-001", "group", "535", "192.0.2.11", "Up", "-", "-", "02:00:00:00:00:01"],
             ["Down-AP", "group", "515", "-", "Down", "-", "-", "02:00:00:00:00:02"]], "Total APs:2")
        state = self.collect_once(session)
        self.assertGreater(state["observed_at"], 0)
        self.assertIn("인증 성공", state["connection"])
        self.assertNotIn("02:00:00:01:00:01", str(state))
        self.assertNotIn("private", str(state))
        tables = state["tables"]
        self.assertIn("Down-AP", str(tables["AP"]))
        self.assertIn("Down", str(tables["AP"]))
        radio = dict(zip(tables["Radio"][0], tables["Radio"][1][0]))
        self.assertEqual([radio[k] for k in ("대역", "채널", "이용률(%)", "간섭(%)", "노이즈(dBm)", "연결 수")],
                         ["5GHz", "44", "94", "5", "-94", "4"])
        bss = dict(zip(tables["SSID / BSSID"][0], tables["SSID / BSSID"][1][0]))
        self.assertEqual([bss[k] for k in ("SSID", "BSS 연결 수", "활성 연결 수")], ["DEMO-GUEST", "1", "1"])
        self.assertTrue(all(len(row) == len(cols) for cols, rows in tables.values() for row in rows))

    def test_zero_associations_and_unavailable_bss_counter_are_explicit(self):
        session = Session()
        session.responses["show ap association"] = table("Association Table",
            ["Name", "bssid", "mac", "auth", "assoc", "essid", "Flags"], [], "Num Clients:0")
        session.responses["show ap bss-table"] = table("Aruba AP BSS Table",
            ["bss","ess","ip","band/ht-mode/bandwidth","ch/EIRP/max-EIRP","type","cur-cl","ap name","fm"],
            [["02:ff:00:01:00:01", "staff", "192.0.2.11", "5GHz/HE/40MHz", "44+/18/24", "ap", "-", "ap", "T"]],
            "Num APs:1")
        tables = self.collect_once(session)["tables"]
        result = dict(zip(tables["SSID / BSSID"][0], tables["SSID / BSSID"][1][0]))
        self.assertEqual((result["BSS 연결 수"], result["활성 연결 수"]), ("미확인", "0"))
        self.assertIn(("활성 연결 수", "0"), tables["장비"][1])

    def test_partial_command_results_do_not_hide_other_supported_tables(self):
        session = Session()
        session.responses["show ap radio-summary"] = "% Permission denied"
        session.responses["show ap association"] = "% Invalid command"
        state = self.collect_once(session)
        self.assertIn("일부", state["cli"])
        self.assertIn("권한 부족", str(state["tables"]["명령 결과"]))
        self.assertIn("미지원", str(state["tables"]["명령 결과"]))
        self.assertIn("DEMO-AP-001", str(state["tables"]["AP"]))
        cols, rows = state["tables"]["Radio"]
        radio = dict(zip(cols, rows[0]))
        self.assertEqual([radio[k] for k in ("대역", "채널", "연결 수", "이용률(%)")], ["5GHz", "44", "4", "미확인"])
        self.assertIn(("활성 연결 수", "미확인"), state["tables"]["장비"][1])
        cols, rows = state["tables"]["SSID / BSSID"]
        self.assertEqual(dict(zip(cols, rows[0]))["활성 연결 수"], "미확인")

    def test_unknown_or_high_cpu_performs_only_the_cpu_command(self):
        for raw, value in (("user 60%, system 10%, idle 30%", "70"), ("unexpected", "미확인"),
                           ("% Permission denied", "미확인"), ("% Invalid command", "미확인")):
            with self.subTest(raw=raw):
                s = Session()
                s.responses["show cpuload"] = raw
                state = self.collect_once(s)
                self.assertEqual(s.calls, ["show cpuload"])
                self.assertIn(("CPU 이용률(%)", value), state["tables"]["장비"][1])
                self.assertIn("보호", state["cli"])

    def test_periodic_uses_one_login_and_refreshes_until_stopped(self):
        opened = []

        def factory(device, password, *, stopped):
            opened.append(Session())
            return opened[-1]

        monitor = self.monitor(factory, interval=.02)
        monitor.start(profile(), "private")
        until(lambda: opened and opened[0].calls.count("show ap radio-summary") >= 2)
        monitor.stop("WLC-1")
        until(lambda: opened[0].closed)
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0].calls.count("show ap database long"), 1)
        self.assertTrue(monitor.state("WLC-1")["stale"])
        self.assertFalse(monitor.state("WLC-1")["running"])

    def test_lost_transport_retains_last_observations_with_a_stale_label(self):
        class Dropped(Session):
            def run(self, name, **kwargs):
                if name == "show cpuload" and name in self.calls:
                    raise OSError("fixture disconnected")
                return super().run(name, **kwargs)

        monitor = self.monitor(lambda p, pw, *, stopped: Dropped(), interval=.02)
        monitor.start(profile(), "private")
        until(lambda: not monitor.state("WLC-1")["running"])
        state = monitor.state("WLC-1")
        self.assertTrue(state["stale"])
        self.assertIn("통신 실패", state["cli"])
        self.assertIn("DEMO-AP-001", str(state["tables"]["AP"]))
        self.assertIn("DEMO-STAFF", str(state["tables"]["SSID / BSSID"]))
        self.assertIn("CLI 통신 실패", str(state["tables"]["명령 결과"]))

    def test_unknown_cpu_keeps_last_good_observation_time_but_hides_other_readings(self):
        class CpuChanged(Session):
            def run(self, name, **kwargs):
                if name == "show cpuload":
                    # First round was CPU-protected, so CPU is the sole previous observation.
                    previous = name in self.calls
                    self.calls.append(name)
                    return "unreadable" if previous else "user 60%, system 10%, idle 30%"
                return super().run(name, **kwargs)

        monitor = self.monitor(lambda p, pw, *, stopped: CpuChanged(), interval=.02)
        monitor.start(profile(), "private")
        until(lambda: "응답 형식" in monitor.state("WLC-1")["cli"])
        state = monitor.state("WLC-1")
        self.assertTrue(state["stale"])
        self.assertIsNotNone(state["observed_at"])
        self.assertIn(("CPU 이용률(%)", "미확인"), state["tables"]["장비"][1])
        self.assertEqual(state["tables"]["Radio"][1], [])

    def test_conflicting_active_associations_keep_the_unique_total_and_unknown_per_bss(self):
        s = Session()
        s.responses["show ap association"] = table("Association Table",
            ["Name", "bssid", "mac", "auth", "assoc", "essid", "Flags"],
            [["DEMO-AP-001", "02:ff:00:01:00:01", "02:00:00:01:00:01", "y", "y", "DEMO-GUEST", "WA"],
             ["DEMO-AP-001", "02:ff:00:01:00:02", "02:00:00:01:00:01", "y", "y", "DEMO-STAFF", "WA"]],
            "Num Clients:2")
        tables = self.collect_once(s)["tables"]
        self.assertIn(("활성 연결 수", "1"), tables["장비"][1])
        cols, rows = tables["SSID / BSSID"]
        self.assertEqual([dict(zip(cols, row))["활성 연결 수"] for row in rows], ["미확인", "미확인", "0", "0"])

    def test_poll_sources_have_times_in_their_own_results(self):
        tables = self.collect_once(Session())["tables"]
        for name, fields in (("AP", ("인벤토리 시각", "활성 목록 시각")),
                             ("Radio", ("Radio 시각", "연결 수 시각")),
                             ("SSID / BSSID", ("BSS 시각", "활성 연결 시각"))):
            cols, rows = tables[name]
            row = dict(zip(cols, rows[0]))
            for col in fields:
                self.assertRegex(row[col], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")

    def test_key_offline_and_post_authentication_shell_errors_have_distinct_results(self):
        for issue, connection, cli in (
                (HostKeyUnapproved(), "신뢰", "미실행"), (HostKeyMismatch(), "비밀번호 전송 전", "미실행"),
                (OSError(), "연결 실패", "미실행"), (DeviceShellUnavailable(), "인증 성공", "CLI 열기 실패")):
            with self.subTest(issue=type(issue).__name__):
                attempts = []
                def factory(p, pw, *, stopped):
                    attempts.append(p)
                    raise issue
                monitor = self.monitor(factory, interval=.01)
                monitor.start(profile(), "private")
                until(lambda: not monitor.state("WLC-1")["running"])
                failed = monitor.state("WLC-1")
                self.assertIn(connection, failed["connection"])
                self.assertIn(cli, failed["cli"])
                self.assertEqual(len(attempts), 1)

    def test_draining_login_reserves_slot_and_target_until_return(self):
        entered = threading.Event()
        release = threading.Event()
        def factory(p, pw, *, stopped):
            entered.set()
            release.wait(3)
            return Session()
        monitor = self.monitor(factory)
        try:
            monitor.start(profile(), "private")
            self.assertTrue(entered.wait(2))
            monitor.forget("WLC-1")
            with self.assertRaises(ValueError):
                monitor.start(DeviceProfile("WLC-2", "192.0.2.1", "readonly"), "private")
            for index in (2, 3, 4):
                monitor.start(profile(index), "private")
            with self.assertRaises(ValueError):
                monitor.start(profile(5), "private")
        finally:
            release.set()

    def test_failed_credentials_do_not_retry_and_other_devices_still_run(self):
        calls = []

        def factory(device, password, *, stopped):
            calls.append(device.host)
            if device.id == "WLC-1":
                raise paramiko.AuthenticationException("private")
            return Session(2)

        monitor = self.monitor(factory, interval=.02)
        monitor.start(profile(), "private")
        monitor.start(profile(2), "private", once=True)
        until(lambda: not any(monitor.state(k)["running"] for k in ("WLC-1", "WLC-2")))
        failed = monitor.state("WLC-1")
        self.assertIn("인증 실패", failed["connection"])
        self.assertIsNone(failed["observed_at"])
        self.assertNotIn("private", str(failed))
        self.assertEqual(calls.count("192.0.2.1"), 1)
        self.assertIn("DEMO-AP-002", str(monitor.state("WLC-2")["tables"]["AP"]))

    def test_replacement_waits_for_pending_connection_and_forget_discards_observations(self):
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        previous, replacement = Session(), Session(2)
        attempts = []

        def factory(device, password, *, stopped):
            attempts.append(password)
            if password == "old":
                entered.set()
                release.wait(3)
                return previous
            self.assertTrue(previous.closed)
            return replacement

        monitor = self.monitor(factory)
        monitor.start(profile(), "old")
        self.assertTrue(entered.wait(2))
        monitor.stop("WLC-1")
        monitor.start(profile(), "new", once=True)
        self.assertEqual(attempts, ["old"])
        release.set()
        until(lambda: not monitor.state("WLC-1")["running"])
        self.assertEqual(previous.calls, [])
        self.assertTrue(replacement.closed)
        self.assertIn("DEMO-AP-002", str(monitor.state("WLC-1")))
        monitor.forget("WLC-1")
        self.assertIsNone(monitor.state("WLC-1")["profile"])
        self.assertIsNone(monitor.state("WLC-1")["observed_at"])

    def test_stopping_pending_login_cancels_old_credentials_and_keeps_replacement_live(self):
        before_auth = threading.Event()
        proceed = threading.Event()
        logins = []

        def factory(device, password, *, stopped=None):
            if password == "old":
                before_auth.set()
                proceed.wait(3)
            if stopped is not None and stopped.is_set():
                raise InterruptedError("stopped before authentication")
            logins.append(password)
            return Session(2)

        monitor = DeviceMonitor(session_factory=factory)
        self.addCleanup(monitor.close)
        try:
            monitor.start(profile(), "old")
            self.assertTrue(before_auth.wait(2))
            monitor.stop("WLC-1")
            monitor.start(profile(), "new", once=True)
        finally:
            proceed.set()
        until(lambda: not monitor.state("WLC-1")["running"])
        self.assertEqual(logins, ["new"])
        self.assertIn("DEMO-AP-002", str(monitor.state("WLC-1")["tables"]["AP"]))

    def test_stop_interrupts_bounded_shell_receive_and_closes_on_worker(self):
        sent = threading.Event()
        closed = threading.Event()
        channel = SimpleNamespace(recv_ready=lambda: False, closed=False, sendall=lambda cmd: sent.set(),
                                  close=lambda: None)
        shell = ShellSession(SimpleNamespace(close=closed.set))
        shell.channel = channel
        monitor = self.monitor(lambda p, password, *, stopped: shell)
        monitor.start(profile(), "private")
        self.assertTrue(sent.wait(2))
        monitor.stop("WLC-1")
        self.assertTrue(closed.wait(1))
        self.assertFalse(monitor.state("WLC-1")["running"])
