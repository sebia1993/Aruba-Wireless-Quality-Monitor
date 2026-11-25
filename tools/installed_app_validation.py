"""Exercise the installed Start menu entry on a disposable Windows runner."""

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener


def wait_for(description, probe, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = probe()
        if value:
            return value
        time.sleep(0.2)
    raise TimeoutError(description)


def validate(entry, application, *, installer=False, repeat=False, progress=lambda phase: None):
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    def window():
        return user32.FindWindowW("AWQM.Desktop", "Aruba Wireless Quality Monitor")

    def snapshot():
        return tuple((p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
                     for p in (application, application.with_name("VERSION.txt")))
    before = snapshot() if repeat else None
    if window():
        raise RuntimeError("An existing dashboard window is open")
    with tempfile.TemporaryDirectory(prefix="awqm-first-launch-") as directory:
        captured = Path(directory) / "browser-url.txt"
        # webbrowser opens this harmless test handler instead of a real browser.
        os.environ["AWQM_CAPTURE_FILE"] = str(captured)
        os.environ["BROWSER"] = f'"{Path(sys.executable).as_posix()}" "{Path(__file__).resolve().as_posix()}" --capture %s'
        if installer:
            # Ordinary double-click semantics: no silent flags, arguments or UI input.
            setup = subprocess.Popen([entry])
            progress(f"installer process started, PID {setup.pid}")
            try:
                hwnd = wait_for("Default setup did not automatically open the dashboard", window, timeout=100)
                progress("application window observed")
                if setup.wait(timeout=15) != 0:
                    raise RuntimeError("Default installation reported an error")
            finally:
                if setup.poll() is None:
                    setup.terminate()
        else:
            os.startfile(entry)
            hwnd = wait_for("Start menu entry did not open the application", window)
        pid = wintypes.DWORD()
        if not user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
            raise ctypes.WinError(ctypes.get_last_error())
        process = kernel.OpenProcess(0x00100000 | 0x1000, False, pid.value)
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            size = wintypes.DWORD(32768)
            image = ctypes.create_unicode_buffer(size.value)
            if not kernel.QueryFullProcessImageNameW(process, 0, image, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not Path(image.value).samefile(application):
                raise RuntimeError("Unexpected executable owns the dashboard")
            try:
                def read_url():
                    if captured.is_file():
                        return captured.read_text(encoding="utf-8").strip()
                address = wait_for("Default dashboard did not request a browser", read_url)
                progress("browser open request observed")
                url = urlsplit(address)
                if url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port:
                    raise RuntimeError("Fresh installation opened an unexpected URL")
                base = f"http://127.0.0.1:{url.port}"
                local = build_opener(ProxyHandler({}))
                with local.open(base + "/", timeout=5) as page:
                    if page.status != 200 or "AWQM" not in page.read().decode("utf-8"):
                        raise RuntimeError("Dashboard page not available")
                with local.open(base + "/api/snapshot", timeout=5) as response:
                    snapshot = json.load(response)
                if not snapshot.get("radios") or not snapshot.get("clients"):
                    raise RuntimeError("Fresh dashboard is missing its initial sample data")
                progress("dashboard response contained sample radio and client data")
                if repeat:
                    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
                    user32.IsIconic.argtypes = [wintypes.HWND]
                    user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
                    duplicate = subprocess.Popen([entry])
                    try:
                        if duplicate.wait(timeout=20) != 0:
                            raise RuntimeError("Repeat download did not exit successfully")
                        wait_for("Existing dashboard was not restored",
                                 lambda: window() == hwnd and not user32.IsIconic(hwnd), timeout=15)
                        current_pid = wintypes.DWORD()
                        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(current_pid))
                        if current_pid.value != pid.value or snapshot() != before:
                            raise RuntimeError("Repeat download replaced the app or opened another instance")
                    finally:
                        if duplicate.poll() is None:
                            duplicate.terminate()
                    progress("Existing window restored without reinstalling")
                print(f"Installed application opened its dashboard: "
                      f"{len(snapshot['radios'])} sample radios, {len(snapshot['clients'])} clients")
            finally:
                if not user32.PostMessageW(hwnd, 0x0010, 0, 0):  # WM_CLOSE
                    raise ctypes.WinError(ctypes.get_last_error())
                if kernel.WaitForSingleObject(process, 20000) != 0:
                    raise RuntimeError("Installed application did not exit when its window closed")
        finally:
            kernel.CloseHandle(process)
    if repeat and snapshot() != before:
        raise RuntimeError("Reopening the download modified the installed program")


def standard_user_trial(installer, receipt, username):
    record = {"passed": False}
    def progress(phase):
        temporary = receipt.with_suffix(".phase.tmp")
        temporary.write_text(phase, encoding="utf-8")
        temporary.replace(receipt.with_suffix(".phase"))
    try:
        progress("standard-user Python started")
        token_user = ctypes.create_unicode_buffer(257)
        size = wintypes.DWORD(len(token_user))
        if not ctypes.windll.advapi32.GetUserNameW(token_user, ctypes.byref(size)):
            raise ctypes.WinError()
        if token_user.value.casefold() != username.casefold() or ctypes.windll.shell32.IsUserAnAdmin():
            raise RuntimeError("Expected the fresh standard-user token")
        def folder(identifier):
            value = ctypes.create_unicode_buffer(260)
            if ctypes.windll.shell32.SHGetFolderPathW(None, identifier, None, 0, value) != 0:
                raise RuntimeError("Could not resolve the standard user profile")
            return Path(value.value)
        profile, local = folder(0x28), folder(0x1C)  # CSIDL_PROFILE, CSIDL_LOCAL_APPDATA
        if not local.is_relative_to(profile) or not profile.is_dir() or not local.is_dir():
            raise RuntimeError("Expected local AppData in the new user profile")
        if (not profile.samefile(os.environ["USERPROFILE"]) or not local.samefile(os.environ["LOCALAPPDATA"])
                or os.environ["USERNAME"].casefold() != username.casefold()):
            raise RuntimeError("Process environment does not match the standard user's loaded profile")
        app = local / "Programs" / "AWQM" / "AWQM.exe"
        if app.exists():
            raise RuntimeError("Existing current-user application detected")
        progress("standard-user identity and fresh application directory verified")
        validate(installer.resolve(strict=True), app, installer=True, progress=progress)
        progress("application exited cleanly")
        record["passed"] = True
    except Exception as error:
        record["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        temporary = receipt.with_suffix(".tmp")
        temporary.write_text(json.dumps(record), encoding="utf-8")
        temporary.replace(receipt)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--capture":
        path = Path(os.environ["AWQM_CAPTURE_FILE"])
        pending = path.with_suffix(".tmp")
        pending.write_text(sys.argv[2], encoding="utf-8")
        pending.replace(path)
    elif len(sys.argv) == 5 and sys.argv[1] == "--install-defaults":
        standard_user_trial(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])
    elif len(sys.argv) == 4 and sys.argv[1] == "--repeat-download":
        validate(Path(sys.argv[2]).resolve(strict=True), Path(sys.argv[3]).resolve(strict=True),
                 installer=True, repeat=True)
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("shortcut", type=Path)
        parser.add_argument("application", type=Path)
        args = parser.parse_args()
        validate(args.shortcut.resolve(strict=True), args.application.resolve(strict=True))
