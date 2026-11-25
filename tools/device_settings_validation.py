"""Exercise the packaged desktop settings against a read-only loopback SSH fixture."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
from contextlib import closing, contextmanager
from urllib.parse import urlsplit, parse_qs
from urllib.request import ProxyHandler, Request, build_opener

import paramiko
import win32con as wc
import win32gui as gui
import win32process

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from awqm.diagnostic import COMMANDS
from awqm.device_window import DeviceWindow as UI
from awqm.fixture_session import fixtures
from awqm.ssh_validation import FixtureInterface, PASSWORD, PROMPT, USER
from tools.installed_app_validation import wait_for


FIRST_HOST, SECOND_HOST = "127.0.0.1", "127.0.0.2"
READ_ONLY_COMMANDS = [name for name, _, _ in COMMANDS]


@contextmanager
def controller(host):
    key = paramiko.RSAKey.generate(2048)

    def change_key():
        nonlocal key
        key = paramiko.RSAKey.generate(2048)

    _, replies = fixtures(2)
    stopped = threading.Event()
    sessions = []
    errors = []
    with socket.socket() as listener:
        listener.bind((host, 22))
        listener.listen()
        listener.settimeout(.2)
        def serve():
            while not stopped.is_set():
                try:
                    peer, _ = listener.accept()
                except TimeoutError:
                    continue
                interface = FixtureInterface()
                interface.disconnected = threading.Event()
                try:
                    with peer, paramiko.Transport(peer) as transport:
                        sessions.append(interface)
                        transport.add_server_key(key)
                        transport.start_server(server=interface)
                        channel = transport.accept(5)
                        if channel is None:
                            continue
                        with channel:
                            if not interface.shell_ready.wait(3):
                                continue
                            channel.settimeout(.2)
                            channel.sendall(PROMPT.encode())
                            buffer = b""
                            while not stopped.is_set():
                                try:
                                    chunk = channel.recv(256)
                                except TimeoutError:
                                    continue
                                if not chunk:
                                    break
                                buffer += chunk
                                while b"\n" in buffer:
                                    line, buffer = buffer.split(b"\n", 1)
                                    name = line.decode().strip()
                                    if name not in READ_ONLY_COMMANDS:
                                        raise ValueError("Unexpected device command")
                                    interface.commands.append(name)
                                    body = replies[name].replace("\n", "\r\n")
                                    channel.sendall((name + "\r\n" + body + "\r\n" + PROMPT).encode())
                except (EOFError, OSError, paramiko.SSHException):
                    # A failed key check or app cancellation intentionally closes the socket.
                    pass
                except Exception as exc:
                    errors.append(str(exc))
                finally:
                    interface.disconnected.set()
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            yield sessions, change_key
        finally:
            stopped.set()
            thread.join(8)
            if thread.is_alive() or errors:
                raise RuntimeError(f"Loopback fixture teardown failed: {errors}")


def process_window(process, kind):
    hwnd = gui.FindWindow(kind, None)
    if hwnd and win32process.GetWindowThreadProcessId(hwnd)[1] == process.pid and gui.IsWindowVisible(hwnd):
        return hwnd
    if process.poll() is not None:
        raise RuntimeError(f"Application exited before expected window ({process.returncode})")


@contextmanager
def running(entry, env):
    if entry.suffix == ".py":
        # Launch the interpreter itself so the HWND belongs to the process under test.
        # Windows venv python.exe may create a child interpreter with another PID.
        import site
        command = [sys._base_executable, str(entry)]
        env = dict(env, PYTHONPATH=os.pathsep.join(site.getsitepackages()))
    else:
        command = [str(entry)]
    proc = subprocess.Popen(command, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        hwnd = wait_for("Desktop did not open", lambda: process_window(proc, "AWQM.Desktop"))
        yield proc, hwnd
    finally:
        if proc.poll() is None:
            hwnd = process_window(proc, "AWQM.Desktop")
            if hwnd:
                gui.PostMessage(hwnd, wc.WM_CLOSE, 0, 0)
            try:
                proc.wait(timeout=25)
            except subprocess.TimeoutExpired:
                proc.terminate()
                proc.wait(timeout=5)
                raise
        if proc.returncode:
            raise RuntimeError(f"Application exit code {proc.returncode}")


def control(hwnd, number):
    handle = gui.GetDlgItem(hwnd, number)
    if not handle:
        raise RuntimeError(f"Missing settings control {number}")
    return handle


def press(hwnd, number):
    handle = control(hwnd, number)
    if not gui.IsWindowEnabled(handle):
        raise RuntimeError(f"Disabled settings control {number}")
    gui.SendMessage(handle, wc.BM_CLICK, 0, 0)


def enter(hwnd, number, value):
    gui.SendMessage(control(hwnd, number), wc.WM_SETTEXT, 0, value)


def read_text(hwnd, number):
    # WM_GETTEXT retrieves a native child control's text across process boundaries.
    handle = control(hwnd, number)
    size = gui.SendMessage(handle, wc.WM_GETTEXTLENGTH, 0, 0) + 1
    buffer = ctypes.create_unicode_buffer(size)
    gui.SendMessage(handle, wc.WM_GETTEXT, size, ctypes.addressof(buffer))
    return buffer.value


def select_combo(hwnd, number, index):
    combo = control(hwnd, number)
    if gui.SendMessage(combo, wc.CB_SETCURSEL, index, 0) == wc.CB_ERR:
        raise RuntimeError(f"Could not select item {index} in control {number}")
    gui.SendMessage(hwnd, wc.WM_COMMAND, (wc.CBN_SELCHANGE << 16) | number, combo)


def select_table(hwnd, name):
    combo = control(hwnd, UI.TABLE)
    index = gui.SendMessage(combo, wc.CB_FINDSTRINGEXACT, -1, name)
    if index == wc.CB_ERR:
        raise RuntimeError(f"Missing results table {name}")
    select_combo(hwnd, UI.TABLE, index)
    return gui.SendMessage(control(hwnd, UI.GRID), 0x1004, 0, 0)  # LVM_GETITEMCOUNT


def collected(sessions, index, description):
    wait_for(description, lambda: len(sessions) > index and
             sessions[index].commands[:len(READ_ONLY_COMMANDS)] == READ_ONLY_COMMANDS)
    session = sessions[index]
    if session.attempts != [USER] or session.shell_requests != 1:
        raise RuntimeError("Unexpected SSH authentication or shell count")
    return session


def check_password_fields(settings):
    if gui.SendMessage(control(settings, UI.PASSWORD), wc.WM_GETTEXTLENGTH, 0, 0) or (
            gui.SendMessage(control(settings, UI.REMEMBER), wc.BM_GETCHECK, 0, 0) != wc.BST_UNCHECKED):
        raise RuntimeError("Expected an empty password field with Remember unchecked")


def check_empty_password(settings):
    check_password_fields(settings)
    press(settings, UI.TEST)
    wait_for("Blank password did not prompt for this controller's password",
             lambda: "비밀번호를 입력" in read_text(settings, UI.STATUS))


def check_account(settings, index, host):
    if gui.SendMessage(control(settings, UI.SLOT), wc.CB_GETCURSEL, 0, 0) != index:
        raise RuntimeError("Unexpected selected WLC")
    if read_text(settings, UI.HOST) != host or read_text(settings, UI.USERNAME) != USER:
        raise RuntimeError("Selected WLC did not display the expected host and username")


def verify(entry, report, wait_for_refresh=False):
    with (tempfile.TemporaryDirectory(prefix="awqm-device-ui-") as temp,
          controller(FIRST_HOST) as (sessions, change_key),
          controller(SECOND_HOST) as (second_sessions, _)):
        root = Path(temp)
        profile = root / "user"
        profile.mkdir()
        capture = root / "browser-url"
        handler = Path(__file__).with_name("installed_app_validation.py").resolve()
        env = dict(os.environ, LOCALAPPDATA=str(profile), USERPROFILE=str(profile), AWQM_CAPTURE_FILE=str(capture),
                   BROWSER=f'"{Path(sys.executable).as_posix()}" "{handler.as_posix()}" --capture %s')
        with running(entry, env) as (proc, hwnd):
            addr = wait_for("Browser entry did not open", lambda: capture.read_text() if capture.exists() else None)
            url = urlsplit(addr)
            origin = f"http://127.0.0.1:{url.port}"
            if url.hostname != "127.0.0.1" or url.scheme != "http":
                raise ValueError("Expected a fresh local dashboard")
            token = parse_qs(url.fragment)["settings"][0]
            opener = build_opener(ProxyHandler({}))
            req = Request(origin + "/api/desktop/settings", method="POST", data=b"",
                          headers={"Origin": origin, "X-AWQM-Settings": token})
            with opener.open(req, timeout=4) as response:
                if not json.load(response)["opened"]:
                    raise RuntimeError("Browser settings request failed")
            settings = wait_for("Browser did not open native settings", lambda: process_window(proc, "AWQM.DeviceSettings"))
            if any(gui.IsWindowVisible(control(settings, cid)) or read_text(settings, cid)
                   for cid in (UI.FINGERPRINT, UI.KNOWN_HOSTS)):
                raise RuntimeError("Fresh setup did not leave advanced SSH trust fields empty and collapsed")
            if gui.SendMessage(control(settings, UI.REMEMBER), wc.BM_GETCHECK, 0, 0) != wc.BST_UNCHECKED:
                raise RuntimeError("Remember must be unchecked at first launch")
            for cid, value in ((UI.HOST, FIRST_HOST), (UI.USERNAME, USER), (UI.PASSWORD, PASSWORD)):
                enter(settings, cid, value)
            press(settings, UI.TEST)
            collected(sessions, 0, "First contact did not complete with only the three login fields")
            press(settings, UI.SETTINGS)
            wait_for("Monitor button did not unlock after connection test",
                     lambda: gui.IsWindowEnabled(control(settings, UI.SAVE)))
            press(settings, UI.SAVE)
            monitor = collected(sessions, 1, "Monitoring did not reconnect to the first fixture")
            press(settings, UI.RESULTS)
            counts = {}
            for table, expected in (("AP", 1), ("Radio", 2), ("SSID / BSSID", 4)):
                wait_for("Wrong row count: " + table, lambda: select_table(settings, table) == expected)
                counts[table] = expected
            if wait_for_refresh:
                wait_for("Periodic radio observations did not refresh",
                         lambda: monitor.commands.count("show ap radio-summary") >= 2, timeout=80)
                if monitor.commands.count("show ap database long") != 1:
                    raise RuntimeError("Full AP inventory was requested again during the first minute")
            press(settings, UI.STOP)
            if not monitor.disconnected.wait(5):
                raise RuntimeError("Stopping monitoring did not close the SSH connection")
            press(settings, UI.NEW)
            check_account(settings, 1, "")
            check_password_fields(settings)
            enter(settings, UI.HOST, SECOND_HOST)
            check_empty_password(settings)
            if second_sessions:
                raise RuntimeError("A new WLC used a password from the previous WLC")
            enter(settings, UI.PASSWORD, PASSWORD)
            press(settings, UI.SAVE)
            second = collected(second_sessions, 0, "Second WLC failed to connect with the prefilled username")
            press(settings, UI.STOP)
            if not second.disconnected.wait(5):
                raise RuntimeError("Stopping the second WLC did not close SSH")
        capture.unlink(missing_ok=True)
        with running(entry, env) as (proc, hwnd):
            settings = wait_for("Saved profile not opened on next launch", lambda: process_window(proc, "AWQM.DeviceSettings"))
            check_account(settings, 0, FIRST_HOST)
            check_empty_password(settings)
            select_combo(settings, UI.SLOT, 1)
            check_account(settings, 1, SECOND_HOST)
            check_empty_password(settings)
            select_combo(settings, UI.SLOT, 0)
            if len(sessions) != 2 or len(second_sessions) != 1 or capture.exists():
                raise RuntimeError("Unexpected automatic connection or sample dashboard on relaunch")
            gui.PostMessage(settings, wc.WM_CLOSE, 0, 0)
            wait_for("Settings did not hide", lambda: not gui.IsWindow(settings) or not gui.IsWindowVisible(settings))
            press(hwnd, 106)
            settings = wait_for("Native settings button did not reopen", lambda: process_window(proc, "AWQM.DeviceSettings"))
            enter(settings, UI.PASSWORD, PASSWORD)
            press(settings, UI.TEST)
            reconnect = collected(sessions, 2, "Same host key failed after restarting the application")
            if not reconnect.disconnected.wait(5):
                raise RuntimeError("Connection test did not disconnect after collecting results")
        # Rotate while the app is closed. An in-memory trust record cannot pass this test.
        change_key()
        with running(entry, env) as (proc, hwnd):
            settings = wait_for("Saved profile unavailable for changed-key check",
                                lambda: process_window(proc, "AWQM.DeviceSettings"))
            check_account(settings, 0, FIRST_HOST)
            enter(settings, UI.PASSWORD, PASSWORD)
            press(settings, UI.TEST)
            mismatch = wait_for("Changed SSH key did not produce a localized mismatch on screen",
                                lambda: "SSH 키 불일치" in (status := read_text(settings, UI.STATUS)) and status)
            details = read_text(settings, UI.DETAILS)
            if "비밀번호 전송 전" not in details or "미실행" not in details:
                raise RuntimeError("Mismatch details did not explain that authentication and commands were stopped")
            wait_for("Changed-key connection did not close", lambda: len(sessions) == 4 and sessions[3].disconnected.is_set())
            rejected = sessions[3]
            if rejected.attempts or rejected.shell_requests or rejected.commands:
                raise RuntimeError("Changed host key reached authentication or a device command")
        if len(sessions) != 4 or len(second_sessions) != 1:
            raise RuntimeError("An unexpected SSH connection was attempted")
        database = profile / "AWQM" / "desktop" / "devices.sqlite3"
        with closing(sqlite3.connect(database.resolve(strict=True).as_uri() + "?mode=ro", uri=True)) as db:
            credentials = db.execute("SELECT id, credential IS NOT NULL FROM profiles ORDER BY id").fetchall()
        if credentials != [("WLC-1", 0), ("WLC-2", 0)]:
            raise RuntimeError("Expected two saved WLC profiles without stored password blobs")
        if any(PASSWORD.encode() in p.read_bytes() for p in profile.rglob("*") if p.is_file()):
            raise RuntimeError("Password persisted in the user profile")
        result = {"fixture": "two loopback SSH controllers", "application": entry.name,
                  "tables": counts, "native_and_web_entries": True,
                  "first_contact_with_three_fields": True, "same_key_reconnect": True,
                  "same_key_after_restart": True, "prefilled_username_new_and_saved_wlc": True,
                  "settings_persisted": True, "session_password_forgotten": True, "password_isolated_by_wlc": True,
                  "stored_password_blobs": 0, "changed_key_status": mismatch,
                  "changed_key_authentication_attempts": len(rejected.attempts),
                  "changed_key_device_commands": len(rejected.commands),
                  "stop_closed_connection": True,
                  "periodic_refresh_verified": wait_for_refresh,
                  "authentication_attempts": sum(len(item.attempts) for item in sessions + second_sessions)}
        if entry.suffix.lower() == ".exe":
            with entry.open("rb") as application:
                result["application_sha256"] = hashlib.file_digest(application, "sha256").hexdigest()
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("application", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--wait-for-refresh", action="store_true")
    args = parser.parse_args()
    try:
        verify(args.application.resolve(strict=True), args.report, args.wait_for_refresh)
    except Exception:
        import traceback
        args.report.with_suffix(".error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        raise
