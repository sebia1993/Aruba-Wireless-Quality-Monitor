"""Exercise the packaged local web settings against two read-only loopback SSH fixtures."""
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
    if hwnd and win32process.GetWindowThreadProcessId(hwnd)[1] == process.pid:
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
                gui.PostMessage(hwnd, wc.WM_COMMAND, 105, 0)  # explicit AWQM exit
            try:
                proc.wait(timeout=25)
            except subprocess.TimeoutExpired:
                proc.terminate()
                proc.wait(timeout=5)
                raise
        if proc.returncode:
            raise RuntimeError(f"Application exit code {proc.returncode}")


def collected(sessions, index, description):
    wait_for(description, lambda: len(sessions) > index and
             sessions[index].commands[:len(READ_ONLY_COMMANDS)] == READ_ONLY_COMMANDS)
    session = sessions[index]
    if session.attempts != [USER] or session.shell_requests != 1:
        raise RuntimeError("Unexpected SSH authentication or shell count")
    return session


def browser_api(capture, expected_path):
    address = wait_for("Browser entry did not open", lambda: capture.read_text() if capture.exists() else None)
    url = urlsplit(address)
    if url.hostname != "127.0.0.1" or url.scheme != "http" or url.path != expected_path:
        raise RuntimeError("Unexpected local dashboard address")
    origin = f"http://127.0.0.1:{url.port}"
    token = parse_qs(url.fragment)["settings"][0]
    opener = build_opener(ProxyHandler({}))
    def call(data):
        req = Request(origin + "/api/desktop/devices", method="POST", data=json.dumps(data).encode(),
                      headers={"Origin": origin, "X-AWQM-Settings": token, "Content-Type":"application/json"})
        with opener.open(req, timeout=5) as response:
            raw = response.read().decode()
        if PASSWORD in raw or token in raw:
            raise RuntimeError("A secret appeared in a browser API response")
        return json.loads(raw)
    return call


def read(call, id="WLC-1", table="AP"):
    return call({"action":"read", "id":id, "table":table, "page":0})


def slot(call, id="WLC-1"):
    return next(item for item in read(call, id)["slots"] if item["id"] == id)


def edit(call, action, id, host, password=PASSWORD):
    return call({"action": action, "id": id, "host": host, "username": USER,
                 "password": password, "remember": False, "fingerprint": "", "known_hosts": ""})


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
            call = browser_api(capture, "/")
            if any(s["profile"] for s in read(call)["slots"]):
                raise RuntimeError("Fresh setup already contains a profile")
            edit(call, "test", "WLC-1", FIRST_HOST)
            first = collected(sessions, 0, "First contact did not complete with only three login fields")
            if not first.disconnected.wait(5):
                raise RuntimeError("Connection test failed to disconnect")
            wait_for("Test did not finish", lambda: not slot(call)["running"])
            edit(call, "save", "WLC-1", FIRST_HOST)
            monitor = collected(sessions, 1, "Monitoring did not reconnect")
            counts = {}
            for table, expected in (("AP", 1), ("Radio", 2), ("SSID / BSSID", 4)):
                wait_for("Wrong row count: " + table,
                         lambda: read(call, "WLC-1", table)["table"]["total"] == expected)
                counts[table] = expected
            if wait_for_refresh:
                # No browser requests during this wait; closing a tab cannot stop collection.
                wait_for("Periodic radio observations did not refresh without browser polling",
                         lambda: monitor.commands.count("show ap radio-summary") >= 2, timeout=80)
                if monitor.commands.count("show ap database long") != 1:
                    raise RuntimeError("Full AP inventory repeated during the first minute")
            call({"action":"stop","id":"WLC-1"})
            if not monitor.disconnected.wait(5):
                raise RuntimeError("Stopping monitoring did not close SSH")
            state = read(call)
            if state["default_username"] != USER or slot(call, "WLC-2")["has_password"]:
                raise RuntimeError("Default username or per-WLC password isolation failed")
            edit(call, "save", "WLC-2", SECOND_HOST)
            second = collected(second_sessions, 0, "Second WLC did not connect")
            call({"action":"stop","id":"WLC-2"})
            if not second.disconnected.wait(5):
                raise RuntimeError("Stopping the second WLC did not close SSH")
            call({"action":"interval","seconds":120})
        capture.unlink(missing_ok=True)
        with running(entry, env) as (proc, hwnd):
            call = browser_api(capture, "/devices")
            state = read(call)
            if state["interval"] != 120 or state["default_username"] != USER:
                raise RuntimeError("Local settings did not survive restart")
            for id, host in (("WLC-1", FIRST_HOST), ("WLC-2", SECOND_HOST)):
                item = slot(call, id)
                if item["profile"]["host"] != host or item["profile"]["username"] != USER or item["has_password"]:
                    raise RuntimeError("Saved account or transient password handling failed")
            if len(sessions) != 2 or len(second_sessions) != 1:
                raise RuntimeError("Unexpected automatic SSH connection on restart")
            edit(call, "test", "WLC-1", FIRST_HOST)
            reconnect = collected(sessions, 2, "Same host key failed after restarting")
            if not reconnect.disconnected.wait(5):
                raise RuntimeError("Connection test did not disconnect")
        capture.unlink(missing_ok=True)
        change_key()
        with running(entry, env) as (proc, hwnd):
            call = browser_api(capture, "/devices")
            edit(call, "test", "WLC-1", FIRST_HOST)
            mismatch = wait_for("Changed key did not produce a localized mismatch",
                                lambda: (s if (s := slot(call))["status"] == "SSH 키 불일치" else None))
            if "비밀번호 전송 전" not in mismatch["connection"] or mismatch["cli"] != "미실행":
                raise RuntimeError("Mismatch did not explain that authentication and commands were stopped")
            wait_for("Changed-key connection did not close", lambda: len(sessions) == 4 and sessions[3].disconnected.is_set())
            rejected = sessions[3]
            if rejected.attempts or rejected.shell_requests or rejected.commands:
                raise RuntimeError("Changed key reached authentication or a device command")
        if len(sessions) != 4 or len(second_sessions) != 1:
            raise RuntimeError("Unexpected SSH connection")
        database = profile / "AWQM" / "desktop" / "devices.sqlite3"
        with closing(sqlite3.connect(database.resolve(strict=True).as_uri() + "?mode=ro", uri=True)) as db:
            credentials = db.execute("SELECT id, credential IS NOT NULL FROM profiles ORDER BY id").fetchall()
        if credentials != [("WLC-1", 0), ("WLC-2", 0)]:
            raise RuntimeError("Expected two profiles without stored passwords")
        if any(PASSWORD.encode() in p.read_bytes() for p in profile.rglob("*") if p.is_file()):
            raise RuntimeError("Password persisted in the user profile")
        result = {"fixture":"two loopback SSH controllers", "application":entry.name,
                  "tables":counts, "web_direct_settings":True, "same_key_after_restart":True,
                  "default_username_and_profiles_persisted":True, "interval_persisted":True,
                  "session_password_forgotten":True, "password_isolated_by_wlc":True,
                  "changed_key_status":mismatch["status"], "changed_key_authentication_attempts":len(rejected.attempts),
                  "changed_key_device_commands":len(rejected.commands), "stop_closed_connection":True,
                  "periodic_refresh_without_browser_polling":wait_for_refresh,
                  "stored_password_blobs":0}
        if entry.suffix.lower() == ".exe":
            with entry.open("rb") as application:
                result["application_sha256"] = hashlib.file_digest(application, "sha256").hexdigest()
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        # The UTF-8 evidence file retains localized text. Escaped ASCII stdout also
        # works on hosted Windows consoles redirected through legacy code pages.
        print(json.dumps(result, ensure_ascii=True))


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
