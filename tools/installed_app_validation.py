"""Exercise the installed Start menu entry on a disposable Windows runner."""

import argparse
from contextlib import closing
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import secrets
import uuid
import sys
import tempfile
import time
from ctypes import wintypes
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener
from urllib.parse import parse_qs


def wait_for(description, probe, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = probe()
        if value:
            return value
        time.sleep(0.2)
    raise TimeoutError(description)


def validate(entry, application, *, installer=False, repeat=False, expected_profile=None, progress=lambda phase: None):
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

    def installed_snapshot():
        return tuple((p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
                     for p in (application, application.with_name("VERSION.txt")))
    before = installed_snapshot() if repeat else None
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
                if expected_profile is not None:
                    if url.path != "/devices":
                        raise RuntimeError("Upgrade did not open the saved-device dashboard")
                    token = parse_qs(url.fragment)["settings"][0]
                    request = Request(base + "/api/desktop/devices", method="POST",
                                      data=json.dumps({"action":"read","id":"WLC-1","table":"AP","page":0}).encode(),
                                      headers={"Origin":base,"X-AWQM-Settings":token,"Content-Type":"application/json"})
                    with local.open(request, timeout=5) as response:
                        state = json.load(response)
                    item = next(x for x in state["slots"] if x["id"] == "WLC-1")
                    if (item["profile"] != expected_profile or not item["remember"] or not item["has_password"]
                            or item["running"] or state["default_username"] != expected_profile["username"]):
                        raise RuntimeError("Migrated profile or DPAPI credential was not available in the installed app")
                    progress("Saved profile and DPAPI credential verified through actual upgraded application")
                if repeat:
                    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
                    user32.IsIconic.argtypes = [wintypes.HWND]
                    user32.IsWindowVisible.argtypes = [wintypes.HWND]
                    user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
                    duplicate = subprocess.Popen([entry])
                    try:
                        if duplicate.wait(timeout=20) != 0:
                            raise RuntimeError("Repeat download did not exit successfully")
                        wait_for("Existing dashboard was not restored",
                                 lambda: window() == hwnd and user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd), timeout=15)
                        current_pid = wintypes.DWORD()
                        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(current_pid))
                        if current_pid.value != pid.value or installed_snapshot() != before:
                            raise RuntimeError("Repeat download replaced the app or opened another instance")
                    finally:
                        if duplicate.poll() is None:
                            duplicate.terminate()
                    progress("Existing window restored without reinstalling")
                print(f"Installed application opened its dashboard: "
                      f"{len(snapshot['radios'])} sample radios, {len(snapshot['clients'])} clients")
            finally:
                if not user32.PostMessageW(hwnd, 0x0111, 105, 0):  # WM_COMMAND: explicit AWQM exit
                    raise ctypes.WinError(ctypes.get_last_error())
                if kernel.WaitForSingleObject(process, 20000) != 0:
                    raise RuntimeError("Installed application did not exit when its window closed")
        finally:
            kernel.CloseHandle(process)
    if repeat and installed_snapshot() != before:
        raise RuntimeError("Reopening the download modified the installed program")


def current_profile():
    """Resolve the profile of the actual process token, not the runner's environment."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    userenv.GetUserProfileDirectoryW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    userenv.GetUserProfileDirectoryW.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        value = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(value))
        if not userenv.GetUserProfileDirectoryW(token, value, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return Path(value.value)
    finally:
        kernel.CloseHandle(token)


def known_folder(identifier, known_id):
    """Initialize real shell folders of a newly loaded test profile."""
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.SHGetFolderPathW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR]
    shell.SHGetFolderPathW.restype = ctypes.c_long
    value = ctypes.create_unicode_buffer(260)
    code = shell.SHGetFolderPathW(None, identifier | 0x8000, None, 0, value)  # CSIDL_FLAG_CREATE
    if code == 0 and value.value:
        return Path(value.value)
    # Fresh users sometimes need the known-folder initialization flags.
    guid = (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(known_id).bytes_le)
    result = ctypes.c_void_p()
    shell.SHGetKnownFolderPath.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE,
                                          ctypes.POINTER(ctypes.c_void_p)]
    shell.SHGetKnownFolderPath.restype = ctypes.c_long
    code2 = shell.SHGetKnownFolderPath(ctypes.byref(guid), 0x8000 | 0x0800, None, ctypes.byref(result))
    try:
        if code2 != 0 or not result.value:
            raise RuntimeError(f"Known folder 0x{identifier:x} failed: HRESULT 0x{code & 0xffffffff:08x}/0x{code2 & 0xffffffff:08x}")
        return Path(ctypes.wstring_at(result.value))
    finally:
        if result.value:
            ole = ctypes.WinDLL("ole32")
            ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
            ole.CoTaskMemFree(result)


def uninstall_current_user(app, local, shortcut):
    import winreg
    registration = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{39E547C5-189F-47E5-AC56-0881E132A87D}_is1"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, registration) as key:
        registered = Path(winreg.QueryValueEx(key, "InstallLocation")[0])
        uninstaller = Path(winreg.QueryValueEx(key, "UninstallString")[0].strip('"'))
    expected = local / "Programs" / "AWQM Setup" / "Uninstall" / "unins000.exe"
    if not registered.samefile(app.parent) or not uninstaller.samefile(expected):
        raise RuntimeError("Unexpected uninstall registration; installation preserved")
    completed = subprocess.run([str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], timeout=90)
    if completed.returncode:
        raise RuntimeError("Uninstall returned a failure")
    wait_for("Uninstall did not remove the app and shortcut",
             lambda: not app.exists() and not shortcut.exists(), timeout=20)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, registration):
            raise RuntimeError("Uninstall registration remains")
    except FileNotFoundError:
        pass


def seed_legacy_profile(local):
    """Use the v0.47 database contract and a real current-user DPAPI blob.

    Only synthetic values; no network connection or real credential.
    """
    from fixture_dpapi import protect
    profile = {"id":"WLC-1","host":"192.0.2.5","username":"fixture-upgrade",
               "fingerprint":"SHA256:" + "A" * 43,"known_hosts":""}
    password = secrets.token_urlsafe(24)
    credential = protect(json.dumps({"profile":profile,"password":password}).encode("utf-8"))
    db_path = local / "AWQM" / "desktop" / "devices.sqlite3"
    if db_path.exists():
        raise RuntimeError("Upgrade fixture requires a fresh device database")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute("CREATE TABLE profiles (id TEXT PRIMARY KEY, host TEXT NOT NULL UNIQUE, username TEXT NOT NULL, fingerprint TEXT NOT NULL, known_hosts TEXT NOT NULL, revision BLOB NOT NULL, credential BLOB)")
        db.execute("CREATE TABLE default_account (id INTEGER PRIMARY KEY CHECK(id=1), username TEXT NOT NULL)")
        db.execute("CREATE TABLE ssh_host_keys (host TEXT NOT NULL, port INTEGER NOT NULL, fingerprint TEXT NOT NULL, PRIMARY KEY(host,port))")
        db.execute("INSERT INTO profiles VALUES (?,?,?,?,?,?,?)",
                   (profile["id"],profile["host"],profile["username"],profile["fingerprint"],profile["known_hosts"],secrets.token_bytes(16),credential))
        db.execute("INSERT INTO default_account VALUES (1,?)",(profile["username"],))
        db.execute("INSERT INTO ssh_host_keys VALUES (?,22,?)",(profile["host"],profile["fingerprint"]))
    return db_path, profile


def legacy_rows(db_path):
    with closing(sqlite3.connect(db_path.resolve(strict=True).as_uri()+"?mode=ro", uri=True)) as db:
        return tuple(db.execute(query).fetchall() for query in (
            "SELECT * FROM profiles ORDER BY id", "SELECT * FROM default_account", "SELECT * FROM ssh_host_keys ORDER BY host,port"))


def standard_user_trial(installer, receipt, username, *, full=False, previous=None):
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
        profile = current_profile()
        progress("standard-user token profile resolved")
        local = known_folder(0x1C, "F1B32785-6FBA-4FCF-9D55-7B8E7F157091")
        roaming = known_folder(0x1A, "3EB685DB-65F9-4CF6-A03A-E3EF65729F3D")
        programs = known_folder(0x02, "A77F5D77-2E2B-44C3-A6A2-ABA601054A51")
        if (not profile.is_dir() or any(not x.is_dir() or not x.resolve().is_relative_to(profile.resolve())
                                         for x in (local, roaming, programs))):
            raise RuntimeError("Expected shell folders in the actual standard user's profile")
        # Only the disposable hosted fixture can normalize inherited shell
        # environment values. The native Windows 11 trial remains strict.
        env = {"USERPROFILE":str(profile), "LOCALAPPDATA":str(local),
               "APPDATA":str(roaming), "USERNAME":username}
        for key, value in env.items():
            previous_value = os.environ.get(key, "")
            matches = (previous_value.casefold() == value.casefold() if key == "USERNAME" else
                       bool(previous_value) and Path(previous_value).exists() and Path(previous_value).samefile(value))
            if not matches:
                if not full:
                    raise RuntimeError("Process environment does not match loaded user profile: " + key)
                os.environ[key] = value
        record["profile_from_token"] = True
        record["shell_folders_initialized"] = True
        app = local / "Programs" / "AWQM" / "AWQM.exe"
        if app.exists():
            raise RuntimeError("Existing current-user application detected")
        progress("standard-user identity and fresh application directory verified")
        validate(installer.resolve(strict=True), app, installer=True, progress=progress)
        progress("application exited cleanly")
        record["first_install"] = True
        if full:
            # Exercise the exact public EXE twice after the initial install,
            # including the duplicate-process/window restoration assertion.
            validate(installer.resolve(strict=True), app, installer=True, repeat=True, progress=progress)
            record["same_download_no_reinstall"] = True
            record["duplicate_window_restored"] = True
            shortcut = programs / "Aruba Wireless Quality Monitor.lnk"
            if not shortcut.is_file():
                raise RuntimeError("Current-user Start menu entry missing")
            validate(shortcut, app, progress=progress)
            record["start_menu_relaunch"] = True
            candidate_version = app.with_name("VERSION.txt").read_text(encoding="utf-8").strip()
            uninstall_current_user(app, local, shortcut)
            record["uninstall_verified"] = True
            if previous is not None:
                progress("installing pinned v0.47.0 baseline")
                if hashlib.sha256(previous.read_bytes()).hexdigest() != "15732fb9827aa7ebae58d36f26d545ca257fa5a9e99ea6974d3ecb100afe75ec":
                    raise RuntimeError("Legacy public installer SHA-256 mismatch")
                result = subprocess.run([str(previous), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER"], timeout=120)
                if result.returncode or app.with_name("VERSION.txt").read_text(encoding="utf-8").strip() != "0.47.0":
                    raise RuntimeError("Pinned baseline installation failed")
                db_path, profile = seed_legacy_profile(local)
                rows = legacy_rows(db_path)
                validate(installer.resolve(strict=True), app, installer=True, expected_profile=profile, progress=progress)
                if app.with_name("VERSION.txt").read_text(encoding="utf-8").strip() != candidate_version:
                    raise RuntimeError("Candidate did not update the old installation")
                if legacy_rows(db_path) != rows:
                    raise RuntimeError("Upgrade modified the original profile, credential or SSH pin")
                record["v047_upgrade_preserves_settings"] = True
                uninstall_current_user(app, local, shortcut)
                if legacy_rows(db_path) != rows:
                    raise RuntimeError("Uninstall removed or changed the user's saved settings")
                record["uninstall_preserves_user_settings"] = True
            record["installer_sha256"] = hashlib.sha256(installer.read_bytes()).hexdigest()
            record["windows_build"] = sys.getwindowsversion().build
            record["windows_product_type"] = sys.getwindowsversion().product_type
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
    elif len(sys.argv) == 6 and sys.argv[1] == "--hosted-standard-user":
        standard_user_trial(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4], full=True, previous=Path(sys.argv[5]))
    elif len(sys.argv) == 4 and sys.argv[1] == "--repeat-download":
        validate(Path(sys.argv[2]).resolve(strict=True), Path(sys.argv[3]).resolve(strict=True),
                 installer=True, repeat=True)
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("shortcut", type=Path)
        parser.add_argument("application", type=Path)
        args = parser.parse_args()
        validate(args.shortcut.resolve(strict=True), args.application.resolve(strict=True))
