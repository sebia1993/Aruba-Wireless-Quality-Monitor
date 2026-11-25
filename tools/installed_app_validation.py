"""Exercise the installed Start menu entry on a disposable Windows runner."""

import argparse
import ctypes
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


def validate(entry, application, *, installer=False):
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
            try:
                hwnd = wait_for("Default setup did not automatically open the dashboard", window, timeout=100)
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
                print(f"Installed application opened its dashboard: "
                      f"{len(snapshot['radios'])} sample radios, {len(snapshot['clients'])} clients")
            finally:
                if not user32.PostMessageW(hwnd, 0x0010, 0, 0):  # WM_CLOSE
                    raise ctypes.WinError(ctypes.get_last_error())
                if kernel.WaitForSingleObject(process, 20000) != 0:
                    raise RuntimeError("Installed application did not exit when its window closed")
        finally:
            kernel.CloseHandle(process)


def standard_user_trial(installer, receipt, username):
    record = {"passed": False}
    try:
        if os.environ.get("USERNAME", "").casefold() != username.casefold() or ctypes.windll.shell32.IsUserAnAdmin():
            raise RuntimeError("Expected the fresh standard-user token")
        app = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "AWQM" / "AWQM.exe"
        if app.exists():
            raise RuntimeError("Existing current-user application detected")
        validate(installer.resolve(strict=True), app, installer=True)
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
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("shortcut", type=Path)
        parser.add_argument("application", type=Path)
        args = parser.parse_args()
        validate(args.shortcut.resolve(strict=True), args.application.resolve(strict=True))
