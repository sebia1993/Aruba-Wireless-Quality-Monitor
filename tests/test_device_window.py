"""Exercise device setup through the actual Windows controls."""
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


class MonitorFixture:
    """Deterministic SSH collector boundary for message-loop tests."""
    def __init__(self):
        self.states = {}
        self.starts = []

    def start(self, profile, password, *, once=False):
        self.starts.append((profile, password, once))
        self.states[profile.id] = dict(profile=profile, status="시험 응답", connection="인증 성공",
            cli="조회 성공", running=not once, stale=False, observed_at=1_700_000_000,
            tables={"AP": (("AP 이름", "관리 IP"), [("연구실-AP", "192.0.2.2")]),
                    "Radio": (("채널",), [("36",), ("40",)])})

    def stop(self, id):
        self.states[id].update(status="중지됨", running=False)

    def state(self, id):
        return self.states.get(id, {})

    def forget(self, id):
        self.states.pop(id, None)


@unittest.skipUnless(sys.platform == "win32", "native Windows UI")
class DeviceWindowTests(unittest.TestCase):
    def setUp(self):
        from awqm import device_window as ui
        from awqm.device_profiles import Profiles
        import win32gui
        import win32con

        self.g, self.c = win32gui, win32con
        temp = self.enterContext(TemporaryDirectory())
        self.profiles = Profiles(Path(temp))
        self.monitor = MonitorFixture()
        self.window = ui.DeviceWindow(0, self.profiles, self.monitor)
        self.addCleanup(self.window.close)
        self.window.show()
        self.w = self.window

    def ctl(self, control):
        return self.g.GetDlgItem(self.w.hwnd, control)

    def button(self, control):
        self.assertTrue(self.g.IsWindowEnabled(self.ctl(control)))
        self.assertTrue(self.g.IsWindowVisible(self.ctl(control)))
        self.g.SendMessage(self.ctl(control), self.c.BM_CLICK, 0, 0)

    def edit(self, control, text):
        self.g.SetWindowText(self.ctl(control), text)

    def enter_login(self, host="192.0.2.1"):
        self.edit(self.w.HOST, host)
        self.edit(self.w.USERNAME, "operator")
        self.edit(self.w.PASSWORD, "fixture password")

    def table(self, name):
        h = self.ctl(self.w.TABLE)
        index = self.g.SendMessage(h, self.c.CB_FINDSTRINGEXACT, -1, name)
        self.assertNotEqual(index, -1)
        self.g.SendMessage(h, self.c.CB_SETCURSEL, index, 0)
        self.g.SendMessage(self.w.hwnd, self.c.WM_COMMAND, self.w.TABLE | self.c.CBN_SELCHANGE << 16, h)

    def test_unsaved_connection_test_populates_real_grid_and_preserves_draft_on_close(self):
        import commctrl
        from win32gui_struct import EmptyLVITEM, UnpackLVITEM
        self.enter_login()
        self.assertTrue(self.g.GetWindowLong(self.ctl(self.w.PASSWORD), self.c.GWL_STYLE) & self.c.ES_PASSWORD)
        self.button(self.w.TEST)
        self.assertTrue(self.monitor.starts[0][2])
        self.assertEqual(self.profiles.list(), [])
        self.table("AP")
        grid = self.ctl(self.w.GRID)
        self.assertEqual(self.g.GetClassName(grid), "SysListView32")
        self.assertEqual(self.g.SendMessage(grid, commctrl.LVM_GETITEMCOUNT), 1)
        for column, expected in enumerate(("연구실-AP", "192.0.2.2")):
            item, buffers = EmptyLVITEM(0, column, mask=commctrl.LVIF_TEXT)
            self.g.SendMessage(grid, commctrl.LVM_GETITEMTEXTW, 0, item)
            self.assertEqual(UnpackLVITEM(item).text, expected)
        self.table("Radio")
        self.assertEqual(self.g.SendMessage(grid, commctrl.LVM_GETITEMCOUNT), 2)
        self.assertIn("2023", self.g.GetWindowText(self.ctl(self.w.OBSERVED)))
        self.g.SendMessage(self.w.hwnd, self.c.WM_CLOSE)
        self.assertFalse(self.g.IsWindowVisible(self.w.hwnd))
        self.w.show()
        self.button(self.w.SETTINGS)
        self.assertEqual(self.g.GetWindowText(self.ctl(self.w.HOST)), "192.0.2.1")

    def test_multiple_targets_do_not_share_results_or_passwords(self):
        import commctrl
        self.enter_login()
        self.button(self.w.SAVE)
        first = self.profiles.list()[0]
        self.assertEqual(self.profiles.password(first), "fixture password")
        self.assertFalse(self.profiles.remembered(first))
        self.button(self.w.NEW)
        self.assertEqual(self.g.GetWindowText(self.ctl(self.w.PASSWORD)), "")
        self.enter_login("192.0.2.3")
        self.button(self.w.SAVE)
        self.button(self.w.STOP)
        self.assertTrue(self.monitor.state("WLC-1")["running"])
        self.assertFalse(self.monitor.state("WLC-2")["running"])
        self.button(self.w.SETTINGS)
        self.edit(self.w.HOST, "192.0.2.4")
        self.button(self.w.TEST)
        self.assertEqual(len(self.monitor.starts), 2)
        self.button(self.w.RESULTS)
        self.assertEqual(self.g.SendMessage(self.ctl(self.w.GRID), commctrl.LVM_GETITEMCOUNT), 0)
        with patch("win32gui.MessageBox", return_value=self.c.IDYES):
            self.button(self.w.REMOVE)
        self.assertEqual([p.id for p in self.profiles.list()], ["WLC-1"])
        self.assertFalse(self.monitor.state("WLC-2"))

    def test_advanced_dashboard_requires_approved_url(self):
        self.button(self.w.ADVANCED)
        self.assertFalse(self.g.IsWindowEnabled(self.ctl(self.w.OPEN_HTTPS)))
        self.w.approved_url = "https://fixture.example/"
        self.w.tick()
        with patch("webbrowser.open", return_value=True) as launch:
            self.button(self.w.OPEN_HTTPS)
        launch.assert_called_once_with("https://fixture.example/")

    def test_revoked_dashboard_cannot_open_from_an_old_displayed_address(self):
        self.w.approved_url = "https://old.example/"
        self.w.resolve_approved_url = lambda: None
        self.button(self.w.ADVANCED)
        with patch("webbrowser.open", side_effect=AssertionError("stale URL opened")) as launch:
            self.button(self.w.OPEN_HTTPS)
        launch.assert_not_called()
        self.assertFalse(self.g.IsWindowEnabled(self.ctl(self.w.OPEN_HTTPS)))

    def test_launcher_can_refresh_profile_count_after_saved_change(self):
        counts = []
        self.w.on_profiles_changed = lambda: counts.append(len(self.profiles.list()))
        self.enter_login()
        self.button(self.w.SAVE)
        with patch("win32gui.MessageBox", return_value=self.c.IDYES):
            self.button(self.w.REMOVE)
        self.assertEqual(counts, [1, 0])

    def test_file_selection_fills_known_hosts_and_cancel_preserves_it(self):
        import pywintypes
        path = self.profiles.root / "known_hosts"
        path.touch()
        responses = [(str(path), "", 0), pywintypes.error(0, "GetOpenFileNameW", "cancelled")]
        with patch("win32gui.GetOpenFileNameW", side_effect=responses) as picker:
            self.button(self.w.BROWSE)
            self.assertEqual(self.g.GetWindowText(self.ctl(self.w.KNOWN_HOSTS)), str(path))
            self.button(self.w.BROWSE)
            self.assertEqual(self.g.GetWindowText(self.ctl(self.w.KNOWN_HOSTS)), str(path))
        self.assertEqual(picker.call_count, 2)
        for invocation in picker.call_args_list:
            self.assertEqual(invocation.kwargs["hwndOwner"], self.w.hwnd)
            self.assertTrue(invocation.kwargs["Flags"] & self.c.OFN_FILEMUSTEXIST)
