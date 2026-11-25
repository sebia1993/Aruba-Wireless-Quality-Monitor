"""Exercise the packaged desktop settings against a read-only loopback SSH fixture."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlsplit, parse_qs
from urllib.request import ProxyHandler, Request, build_opener

import paramiko
import win32con as wc
import win32gui as gui
import win32process

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from awqm.diagnostic import COMMANDS
from awqm.fixture_session import fixtures
from awqm.ssh_validation import FixtureInterface, PASSWORD, PROMPT, USER
from tools.installed_app_validation import wait_for


@contextmanager
def controller():
    key = paramiko.RSAKey.generate(2048)
    pin = "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
    allowed = {name for name, _, _ in COMMANDS}
    _, replies = fixtures(2)
    stopped = threading.Event()
    sessions = []
    errors = []
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 22))
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
                                    if name not in allowed:
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
            yield pin, sessions
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


def select_table(hwnd, name):
    combo = control(hwnd, 222)
    index = gui.SendMessage(combo, wc.CB_FINDSTRINGEXACT, -1, name)
    if index == wc.CB_ERR:
        raise RuntimeError(f"Missing results table {name}")
    gui.SendMessage(combo, wc.CB_SETCURSEL, index, 0)
    gui.SendMessage(hwnd, wc.WM_COMMAND, (wc.CBN_SELCHANGE << 16) | 222, combo)
    return gui.SendMessage(control(hwnd, 223), 0x1004, 0, 0)  # LVM_GETITEMCOUNT


def verify(entry, report, wait_for_refresh=False):
    with tempfile.TemporaryDirectory(prefix="awqm-device-ui-") as temp, controller() as (pin, sessions):
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
            for cid, value in ((210, "127.0.0.1"), (211, USER), (212, PASSWORD), (214, pin)):
                enter(settings, cid, value)
            press(settings, 217)
            wait_for("Read-only connection test did not complete", lambda: sessions and len(sessions[0].commands) == 6)
            press(settings, 203)
            wait_for("Monitor button did not unlock after connection test", lambda: gui.IsWindowEnabled(control(settings, 218)))
            press(settings, 218)
            wait_for("Monitoring did not collect the fixture", lambda: len(sessions) >= 2 and len(sessions[1].commands) == 6)
            press(settings, 204)
            counts = {}
            for table, expected in (("AP", 1), ("Radio", 2), ("SSID / BSSID", 4)):
                wait_for("Wrong row count: " + table, lambda: select_table(settings, table) == expected)
                counts[table] = expected
            if wait_for_refresh:
                wait_for("Periodic radio observations did not refresh",
                         lambda: sessions[1].commands.count("show ap radio-summary") >= 2, timeout=80)
                if sessions[1].commands.count("show ap database long") != 1:
                    raise RuntimeError("Full AP inventory was requested again during the first minute")
            press(settings, 219)
            if not sessions[1].disconnected.wait(5):
                raise RuntimeError("Stopping monitoring did not close the SSH connection")
            if any(len(item.attempts) != 1 for item in sessions):
                raise RuntimeError("Unexpected login attempts")
            # Never store the synthetic password without selecting Remember.
            if not any(profile.rglob("*.sqlite3")):
                raise RuntimeError("App profile did not stay inside the isolated user folder")
            if any(PASSWORD.encode() in p.read_bytes() for p in profile.rglob("*") if p.is_file()):
                raise RuntimeError("Password persisted in the user profile")
        capture.unlink(missing_ok=True)
        with running(entry, env) as (proc, hwnd):
            settings = wait_for("Saved profile not opened on next launch", lambda: process_window(proc, "AWQM.DeviceSettings"))
            if gui.SendMessage(control(settings, 210), wc.WM_GETTEXTLENGTH, 0, 0) != len("127.0.0.1"):
                raise RuntimeError("Relaunched window did not select the saved controller")
            if gui.SendMessage(control(settings, 212), wc.WM_GETTEXTLENGTH, 0, 0) != 0 or capture.exists():
                raise RuntimeError("Unexpected saved password or sample dashboard on relaunch")
            gui.PostMessage(settings, wc.WM_CLOSE, 0, 0)
            wait_for("Settings did not hide", lambda: not gui.IsWindow(settings) or not gui.IsWindowVisible(settings))
            press(hwnd, 106)
            wait_for("Native settings button did not reopen", lambda: process_window(proc, "AWQM.DeviceSettings"))
        result = {"fixture": "loopback SSH", "tables": counts, "native_and_web_entries": True,
                  "settings_persisted": True, "session_password_forgotten": True,
                  "stop_closed_connection": True,
                  "periodic_refresh_verified": wait_for_refresh,
                  "authentication_attempts": sum(len(item.attempts) for item in sessions)}
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
