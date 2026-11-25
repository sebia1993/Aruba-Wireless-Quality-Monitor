"""Windows first-run dashboard and service controls."""
from contextlib import ExitStack
import os
from pathlib import Path
import sys
import threading
import webbrowser

from .desktop_endpoint import approved_endpoint
from .web import demo_server


TITLE = "Aruba Wireless Quality Monitor"
WINDOW_CLASS = "AWQM.Desktop"


class Dashboard:
    def __init__(self):
        self.resources = ExitStack()
        self.demo_port = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.resources.close()

    def select(self, approved_url):
        if approved_url:
            return approved_url
        if self.demo_port is None:
            server = self.resources.enter_context(demo_server(0))
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
            thread.start()
            self.resources.callback(thread.join)
            self.resources.callback(server.shutdown)
            self.demo_port = server.server_port
        return f"http://127.0.0.1:{self.demo_port}/"


def service_state():
    import pywintypes
    import win32serviceutil
    try:
        state = win32serviceutil.QueryServiceStatus("AWQM")[1]
    except pywintypes.error as exc:
        return "missing" if exc.winerror == 1060 else "unknown"
    return {1: "stopped", 2: "starting", 3: "stopping", 4: "running"}.get(state, "other")


def installed_program(portable):
    if portable or not getattr(sys, "frozen", False):
        return None
    from win32com.shell import shell, shellcon
    program_files = Path(shell.SHGetFolderPath(0, shellcon.CSIDL_PROGRAM_FILES, None, 0))
    program = program_files / "AWQM" / "AWQM.exe"
    if program.is_file() and program.resolve() == program:
        return program
    return None


def verify_manager(program):
    from functools import partial
    from .update_snapshot import _walk
    from .windows_security import check_program_acl
    if program != installed_program(False) or program is None:
        raise ValueError("A protected machine installation is required")
    # A desktop user is not a trusted writer for an executable about to be elevated.
    check = partial(check_program_acl, trust_current_owner=False)
    check(program.parent.parent)
    _, files, _ = _walk(program.parent, check)
    required = {"AWQM.exe", "_internal/base_library.zip", "_internal/sqlite3.dll", "_internal/python314.dll"}
    if not required.issubset(files):
        raise ValueError("Incomplete machine installation")


def prepared_service():
    from .service_config import default_root
    try:
        (default_root() / "config" / "service.json").stat()
    except FileNotFoundError:
        return False
    except PermissionError:
        # The operator's desktop need not have access to the service-account configuration.
        pass
    return True


def hide_own_console():
    """Explorer creates a console for the dual-purpose EXE; preserve an existing terminal."""
    if not getattr(sys, "frozen", False):
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.windll.kernel32
    processes = (wintypes.DWORD * 4)()
    count = kernel.GetConsoleProcessList(processes, len(processes))
    if count != 1 or processes[0] != os.getpid():
        return
    kernel.GetConsoleWindow.restype = wintypes.HWND
    hwnd = kernel.GetConsoleWindow()
    if hwnd:
        ctypes.windll.user32.ShowWindow(wintypes.HWND(hwnd), 0)


class DesktopWindow:
    OPEN, REFRESH, START, STOP, INSTALL, CLOSE = range(100, 106)
    STATUS, MODE, ADDRESS, NOTICE = range(110, 114)
    states = {"missing": "미등록", "stopped": "중지됨", "running": "실행 중", "starting": "시작 중",
              "stopping": "중지 중", "other": "전환 중", "unknown": "확인할 수 없음"}

    def __init__(self, dashboard, *, installed, smoke=False):
        import win32api
        import win32con as c
        import win32gui as g
        self.dashboard = dashboard
        self.installed = installed
        self.smoke = smoke
        self.manager = None
        self.state = "unknown"
        self.controls = {}
        self.instance = win32api.GetModuleHandle(None)
        self.class_name = WINDOW_CLASS + (".Smoke" if smoke else "")
        wc = g.WNDCLASS()
        wc.hInstance = self.instance
        wc.lpszClassName = self.class_name
        wc.lpfnWndProc = self.message
        wc.hCursor = g.LoadCursor(0, c.IDC_ARROW)
        wc.hbrBackground = c.COLOR_BTNFACE + 1
        g.RegisterClass(wc)
        self.hwnd = None
        try:
            self.hwnd = g.CreateWindowEx(c.WS_EX_CONTROLPARENT, self.class_name, TITLE,
                c.WS_CAPTION | c.WS_SYSMENU | c.WS_MINIMIZEBOX, c.CW_USEDEFAULT, c.CW_USEDEFAULT,
                560, 267, 0, 0, self.instance, None)
            def child(kind, control, text, x, y, w, h, style=0):
                handle = g.CreateWindowEx(0, kind, text, c.WS_CHILD | c.WS_VISIBLE | style,
                    x, y, w, h, self.hwnd, control, self.instance, None)
                g.SendMessage(handle, c.WM_SETFONT, g.GetStockObject(17), True)
                self.controls[control] = handle

            child("STATIC", self.MODE, "", 18, 14, 515, 23)
            child("EDIT", self.ADDRESS, "", 18, 42, 510, 24, c.ES_READONLY | c.ES_AUTOHSCROLL | c.WS_BORDER | c.WS_TABSTOP)
            child("BUTTON", self.OPEN, "대시보드 열기", 18, 76, 122, 30, c.WS_TABSTOP)
            child("STATIC", self.STATUS, "", 18, 124, 184, 23)
            for index, (action, caption) in enumerate(((self.REFRESH, "새로 고침"), (self.START, "시작"),
                                                       (self.STOP, "중지"), (self.INSTALL, "등록 안내"))):
                child("BUTTON", action, caption, 206 + index * 81, 118, 78, 28, c.WS_TABSTOP)
            child("STATIC", self.NOTICE, "", 18, 161, 505, 36)
            child("BUTTON", self.CLOSE, "닫기", 444, 194, 84, 27, c.WS_TABSTOP)
        except Exception:
            self.close()
            raise

    def close(self):
        import win32gui as g
        if self.manager is not None:
            self.manager.Close()
            self.manager = None
        if self.hwnd and g.IsWindow(self.hwnd):
            g.DestroyWindow(self.hwnd)
        g.UnregisterClass(self.class_name, self.instance)

    def refresh(self):
        import win32gui as g
        self.state = "missing" if self.smoke else service_state()
        self.live = None if self.smoke else approved_endpoint(self.state)
        self.url = self.dashboard.select(self.live)
        g.SetWindowText(self.controls[self.MODE], "운영 대시보드 · 승인된 HTTPS 주소" if self.live
                        else "모의 대시보드 · 합성 데이터로 표시하며 현장 장비에 연결하지 않습니다.")
        g.SetWindowText(self.controls[self.ADDRESS], self.url)
        g.SetWindowText(self.controls[self.STATUS], "이 PC의 서비스  ·  " + self.states[self.state])
        g.EnableWindow(self.controls[self.START], bool(self.installed) and self.state == "stopped" and self.manager is None)
        g.EnableWindow(self.controls[self.STOP], bool(self.installed) and self.state == "running" and self.manager is None)
        g.EnableWindow(self.controls[self.INSTALL], self.state == "missing" and self.manager is None)
        g.SetWindowText(self.controls[self.INSTALL], "서비스 등록" if self.installed and self.state == "missing"
                        and prepared_service() else "등록 안내")

    def open(self):
        import win32gui as g
        self.refresh()
        try:
            opened = webbrowser.open(self.url)
        except (OSError, webbrowser.Error):
            opened = False
        note = ("브라우저에서 운영 화면을 확인하세요." if self.live else "이 창을 닫으면 모의 대시보드가 종료됩니다.")
        if not opened:
            note = "브라우저를 열 수 없습니다. 위 주소를 복사해 브라우저에서 여세요."
        g.SetWindowText(self.controls[self.NOTICE], note)

    def manage(self, action):
        import pywintypes
        import win32con as c
        import win32gui as g
        from win32com.shell import shell, shellcon
        if action == "install" and (not self.installed or not prepared_service()):
            g.MessageBox(self.hwnd, "서비스 등록에는 이 PC에 설치한 AWQM과 관리자가 준비한 전용 서비스 계정 및 "
                         "승인된 컨트롤러 설정이 필요합니다. 배포 폴더의 docs\\WINDOWS_SERVICE.md에서 절차를 확인하세요.",
                         "서비스 등록 안내", c.MB_ICONINFORMATION)
            return
        if not self.installed or self.manager is not None:
            return
        if action == "stop" and g.MessageBox(self.hwnd, "이 PC의 AWQM 수집 서비스를 중지할까요?", TITLE,
                                              c.MB_YESNO | c.MB_ICONQUESTION | c.MB_DEFBUTTON2) != c.IDYES:
            return
        try:
            verify_manager(self.installed)
        except (OSError, ValueError, pywintypes.error):
            g.SetWindowText(self.controls[self.NOTICE], "관리 도구의 설치 경로와 쓰기 권한을 검증할 수 없습니다. 컴퓨터 전체 설치를 확인하세요.")
            return
        try:
            result = shell.ShellExecuteEx(fMask=shellcon.SEE_MASK_NOCLOSEPROCESS, hwnd=self.hwnd,
                lpVerb="runas", lpFile=str(self.installed), lpParameters="--manager " + action,
                lpDirectory=str(self.installed.parent), nShow=c.SW_SHOWNORMAL if action == "install" else c.SW_HIDE)
            self.manager = result["hProcess"]
            self.refresh()
            g.SetWindowText(self.controls[self.NOTICE], "Windows 서비스 작업을 요청했습니다.")
        except pywintypes.error as exc:
            notice = "권한 요청을 취소했습니다." if exc.winerror == 1223 else "서비스 관리 창을 열 수 없습니다."
            g.SetWindowText(self.controls[self.NOTICE], notice)

    def tick(self):
        import win32event
        import win32gui as g
        import win32process
        if self.manager is not None and win32event.WaitForSingleObject(self.manager, 0) == win32event.WAIT_OBJECT_0:
            code = win32process.GetExitCodeProcess(self.manager)
            self.manager.Close()
            self.manager = None
            self.refresh()
            text = "서비스 요청을 완료했습니다." if code == 0 else "요청을 완료하지 못했습니다. 서비스 계정, 권한과 설정을 확인하세요."
            g.SetWindowText(self.controls[self.NOTICE], text)
        elif self.state in ("starting", "stopping"):
            self.refresh()

    def message(self, hwnd, msg, wparam, lparam):
        import win32con as c
        import win32gui as g
        if msg == c.WM_COMMAND:
            control = wparam & 0xffff
            if control == self.OPEN:
                self.open()
            elif control == self.REFRESH:
                self.refresh()
            elif control in (self.START, self.STOP, self.INSTALL):
                self.manage({self.START: "start", self.STOP: "stop", self.INSTALL: "install"}[control])
            elif control in (self.CLOSE, c.IDCANCEL):
                g.DestroyWindow(hwnd)
            return 0
        if msg == c.WM_TIMER:
            self.tick()
            return 0
        if msg == c.WM_DESTROY:
            if not self.smoke:
                g.PostQuitMessage(0)
            return 0
        return g.DefWindowProc(hwnd, msg, wparam, lparam)

    def run(self):
        import ctypes
        from ctypes import wintypes
        import win32con as c
        import win32gui as g
        g.ShowWindow(self.hwnd, c.SW_SHOWNORMAL)
        self.open()
        user32 = ctypes.windll.user32
        hwnd = wintypes.HWND(self.hwnd)
        user32.SetTimer(hwnd, 1, 1000, None)
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            if not user32.IsDialogMessageW(hwnd, ctypes.byref(message)):
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))


def run_desktop(portable):
    import time
    import pywintypes
    import win32api
    import win32con as c
    import win32event
    import win32gui as g
    from .windows_security import current_sid
    mutex = win32event.CreateMutex(None, False, "Local\\AWQM.Desktop." + current_sid())
    duplicate = win32api.GetLastError() == 183
    try:
        hide_own_console()
        if duplicate:
            for _ in range(30):
                existing = g.FindWindow(WINDOW_CLASS, TITLE)
                if existing:
                    g.ShowWindow(existing, c.SW_RESTORE)
                    try:
                        g.SetForegroundWindow(existing)
                    except pywintypes.error:
                        g.FlashWindow(existing, True)
                    break
                time.sleep(0.1)
            return
        with Dashboard() as dashboard:
            window = DesktopWindow(dashboard, installed=installed_program(portable))
            try:
                window.run()
            finally:
                window.close()
    finally:
        mutex.Close()


def desktop_smoke():
    import json
    from urllib.request import ProxyHandler, build_opener
    import win32gui
    with Dashboard() as dashboard:
        window = DesktopWindow(dashboard, installed=False, smoke=True)
        try:
            window.refresh()
            if win32gui.IsWindowVisible(window.hwnd) or not win32gui.IsWindow(window.controls[window.OPEN]):
                raise RuntimeError("Hidden desktop controls failed to initialize")
            opener = build_opener(ProxyHandler({}))
            with opener.open(window.url + "api/snapshot", timeout=5) as response:
                radios = len(json.load(response)["radios"])
            if not radios:
                raise RuntimeError("Desktop demo did not publish its inventory")
            return {"native_controls": True, "loopback_dashboard": True, "radios": radios}
        finally:
            window.close()
