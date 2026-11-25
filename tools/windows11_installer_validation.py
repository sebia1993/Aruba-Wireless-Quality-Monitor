"""Qualify the actual no-argument installer on a clean, non-elevated Windows 11 profile."""
import argparse
import ctypes
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid
import winreg
from ctypes import wintypes

import win32api
import win32con
import win32event
import win32process
from win32com.shell import shell, shellcon

REGISTRATION = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{39E547C5-189F-47E5-AC56-0881E132A87D}_is1"


def registration():
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRATION)
    except FileNotFoundError:
        return None
    with key:
        return {name: winreg.QueryValueEx(key, name)[0]
                for name in ("DisplayName", "InstallLocation", "UninstallString")}


def run_on_desktop(name, command, directory, timeout):
    startup = win32process.STARTUPINFO()
    startup.lpDesktop = "WinSta0\\" + name
    process, thread, _, _ = win32process.CreateProcess(
        str(command[0]), subprocess.list2cmdline(list(map(str, command))), None, None,
        False, win32con.CREATE_NO_WINDOW, None, str(directory), startup)
    thread.Close()
    try:
        if win32event.WaitForSingleObject(process, timeout * 1000) != win32event.WAIT_OBJECT_0:
            win32process.TerminateProcess(process, 1)
            raise TimeoutError("Verification process did not exit within its time limit")
        if win32process.GetExitCodeProcess(process) != 0:
            raise RuntimeError("Verification process failed; inspect the first-launch report")
    finally:
        process.Close()


def verify(installer, expected_digest, output, device_settings=False):
    system = sys.getwindowsversion()
    if system.product_type != 1 or system.build < 22000 or ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError("Run on Windows 11 under a non-elevated account")
    if installer.name != "AWQM-Setup.exe" or not installer.is_file():
        raise ValueError("Expected AWQM-Setup.exe")
    with installer.open("rb") as file:
        if hashlib.file_digest(file, "sha256").hexdigest() != expected_digest.lower():
            raise ValueError("Installer digest mismatch")
    local = Path(shell.SHGetFolderPath(0, shellcon.CSIDL_LOCAL_APPDATA, None, 0))
    app = local / "Programs" / "AWQM" / "AWQM.exe"
    metadata = local / "Programs" / "AWQM Setup"
    shortcut = Path(shell.SHGetFolderPath(0, shellcon.CSIDL_PROGRAMS, None, 0)) / "Aruba Wireless Quality Monitor.lnk"
    if any(path.exists() for path in (app.parent, metadata, shortcut)) or registration():
        raise RuntimeError("A clean AWQM user profile is required")

    output.mkdir(parents=True, exist_ok=False)
    helper = Path(__file__).with_name("installed_app_validation.py").resolve(strict=True)
    name = "AWQMInstallTest" + uuid.uuid4().hex
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.CreateDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p,
                                       wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    user32.CreateDesktopW.restype = wintypes.HANDLE
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    desktop = user32.CreateDesktopW(name, None, None, 0, 0x01ff, None)
    if not desktop:
        raise ctypes.WinError(ctypes.get_last_error())
    receipt = output / "first-launch.json"
    try:
        try:
            run_on_desktop(name, [sys.executable, helper, "--install-defaults", installer,
                                  receipt, win32api.GetUserName()], output, 145)
            if json.loads(receipt.read_text(encoding="utf-8")).get("passed") is not True:
                raise RuntimeError("Default dashboard launch was not observed")
            run_on_desktop(name, [sys.executable, helper, shortcut, app], output, 85)
            if device_settings:
                trial = Path(__file__).with_name("device_settings_validation.py").resolve(strict=True)
                run_on_desktop(name, [sys.executable, trial, app, output / "device-settings.json",
                                     "--wait-for-refresh"], output, 210)
        finally:
            record = registration()
            if record:
                remove = Path(record["UninstallString"].strip('"')).resolve(strict=True)
                if (remove != (metadata / "Uninstall" / "unins000.exe").resolve()
                        or not record["DisplayName"].startswith("Aruba Wireless Quality Monitor")
                        or Path(record["InstallLocation"]).resolve() != app.parent.resolve()):
                    raise RuntimeError("Unexpected uninstall registration; installation preserved")
                run_on_desktop(name, [remove, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], output, 90)
            deadline = time.monotonic() + 15
            while (app.exists() or shortcut.exists() or registration()) and time.monotonic() < deadline:
                time.sleep(.25)
            if app.exists() or shortcut.exists() or registration():
                raise RuntimeError("Test installation cleanup could not be verified")
    finally:
        user32.CloseDesktop(desktop)
    result = {"status": "pass", "windows_build": system.build, "installer_sha256": expected_digest,
              "no_argument_install": True, "dashboard_data": True, "start_menu_relaunch": True,
              "uninstall_verified": True}
    if device_settings:
        result["device_settings"] = json.loads((output / "device-settings.json").read_text(encoding="utf-8"))
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("installer", type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--device-settings", action="store_true", help="Also exercise in-app setup against loopback SSH")
    args = parser.parse_args()
    verify(args.installer.resolve(strict=True), args.sha256, args.output.resolve(), args.device_settings)
