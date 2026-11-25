"""Current-user controller profiles and verified, password-only SSH sessions."""
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from threading import RLock
import errno
import hmac
import ipaddress
import json
import os
import re
import secrets
import select
import socket
import sqlite3
import sys
import time

from . import dpapi
from .collector import HostKeyUnapproved, PinnedSSH
from .service_config import ID, USER
from .ssh_shell import ShellSession


def _validate_username(username):
    if not isinstance(username, str) or not USER.fullmatch(username):
        raise ValueError("조회 계정 이름을 확인하세요.")


@dataclass(frozen=True)
class DeviceProfile:
    id: str
    host: str
    username: str
    fingerprint: str = ""
    known_hosts: str = ""

    def __post_init__(self):
        if not isinstance(self.id, str) or not ID.fullmatch(self.id):
            raise ValueError("장비 이름을 확인하세요.")
        _validate_username(self.username)
        try:
            if not isinstance(self.host, str) or "%" in self.host:
                raise ValueError
            address = ipaddress.ip_address(self.host)
            if address.is_unspecified or address.is_multicast or str(address) == "255.255.255.255":
                raise ValueError
        except ValueError:
            raise ValueError("장비의 관리 IPv4 또는 IPv6 주소를 입력하세요.") from None
        object.__setattr__(self, "host", str(address))
        if not isinstance(self.fingerprint, str) or (
                self.fingerprint and not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]", self.fingerprint)):
            raise ValueError("확인한 OpenSSH SHA256 지문을 입력하세요.")
        path = self.known_hosts
        if not isinstance(path, str) or (path and (
                "\x00" in path or path.startswith(("\\\\", "//")) or not Path(path).is_absolute())):
            raise ValueError("로컬 known_hosts 파일을 선택하세요.")


def _validate_password(password, *, required):
    if not isinstance(password, str) or len(password) > 1024 or (required and not password):
        raise ValueError("조회 계정 비밀번호를 확인하세요.")


_COLUMNS = "id, host, username, fingerprint, known_hosts"


def _store_default_account(db, username, *, replace=False):
    db.execute("""INSERT INTO default_account (id, username) VALUES (1, ?)
        ON CONFLICT(id) DO UPDATE SET username=excluded.username WHERE ?""", (username, replace))


class Profiles:
    """One instance owns the passwords retained during the desktop session."""

    def __init__(self, root=None):
        if root is None:
            local = os.environ.get("LOCALAPPDATA")
            if not local:
                local = Path.home() / ("AppData/Local" if sys.platform == "win32" else ".local/share")
            root = Path(local) / "AWQM" / "desktop"
        self.root = Path(root)
        self._transient = {}
        self._lock = RLock()

    @contextmanager
    def _database(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with closing(sqlite3.connect(self.root / "devices.sqlite3")) as db, db:
                db.execute("PRAGMA secure_delete=ON")
                db.execute("""CREATE TABLE IF NOT EXISTS profiles (
                    id TEXT PRIMARY KEY, host TEXT NOT NULL UNIQUE, username TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, known_hosts TEXT NOT NULL, revision BLOB NOT NULL,
                    credential BLOB)""")
                if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='default_account'").fetchone():
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("""CREATE TABLE IF NOT EXISTS default_account (
                        id INTEGER PRIMARY KEY CHECK (id=1), username TEXT NOT NULL)""")
                    # Older installations have only per-device accounts. Keep those rows and credentials intact.
                    db.execute("""INSERT OR IGNORE INTO default_account (id, username)
                        SELECT 1, username FROM profiles ORDER BY id LIMIT 1""")
                    db.commit()
                _initialize_host_pins(db)
                yield db
        except sqlite3.IntegrityError:
            raise ValueError("같은 관리 IP의 장비가 이미 등록되어 있습니다.") from None
        except sqlite3.Error:
            raise RuntimeError("현재 사용자의 장비 설정을 읽거나 저장할 수 없습니다.") from None

    def list(self):
        try:
            (self.root / "devices.sqlite3").stat()
        except FileNotFoundError:
            return []
        with self._database() as db:
            return [DeviceProfile(*row) for row in db.execute(f"SELECT {_COLUMNS} FROM profiles ORDER BY id")]

    def poll_interval(self):
        if not (self.root / "devices.sqlite3").exists():
            return 60
        with self._lock, self._database() as db:
            db.execute("CREATE TABLE IF NOT EXISTS desktop_settings (id INTEGER PRIMARY KEY CHECK(id=1), interval INTEGER NOT NULL)")
            row = db.execute("SELECT interval FROM desktop_settings WHERE id=1").fetchone()
            value = row[0] if row else 60
            if type(value) is not int or not 60 <= value <= 600:
                raise ValueError("저장된 수집 주기를 확인하세요.")
            return value

    def set_poll_interval(self, seconds):
        if type(seconds) is not int or not 60 <= seconds <= 600:
            raise ValueError("수집 주기는 60~600초 사이의 정수로 입력하세요.")
        with self._lock, self._database() as db:
            db.execute("CREATE TABLE IF NOT EXISTS desktop_settings (id INTEGER PRIMARY KEY CHECK(id=1), interval INTEGER NOT NULL)")
            db.execute("INSERT INTO desktop_settings(id, interval) VALUES(1, ?) ON CONFLICT(id) DO UPDATE SET interval=excluded.interval", (seconds,))

    def alerts_enabled(self):
        if not (self.root / "devices.sqlite3").exists():
            return False
        with self._lock, self._database() as db:
            db.execute("CREATE TABLE IF NOT EXISTS desktop_alerts (id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL CHECK(enabled IN (0,1)))")
            row = db.execute("SELECT enabled FROM desktop_alerts WHERE id=1").fetchone()
            return bool(row[0]) if row else False

    def set_alerts_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("알림 설정을 확인하세요.")
        with self._lock, self._database() as db:
            db.execute("CREATE TABLE IF NOT EXISTS desktop_alerts (id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL CHECK(enabled IN (0,1)))")
            db.execute("INSERT INTO desktop_alerts(id, enabled) VALUES(1, ?) ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled", (int(enabled),))

    def default_username(self):
        if not (self.root / "devices.sqlite3").exists():
            return ""
        with self._database() as db:
            row = db.execute("SELECT username FROM default_account WHERE id=1").fetchone()
            return row[0] if row else ""

    def set_default_username(self, username, *, replace=False):
        _validate_username(username)
        with self._database() as db:
            _store_default_account(db, username, replace=replace)
            return db.execute("SELECT username FROM default_account WHERE id=1").fetchone()[0]

    def save(self, profile, password, remember):
        if not isinstance(profile, DeviceProfile) or not isinstance(remember, bool):
            raise ValueError("장비 설정을 확인하세요.")
        _validate_password(password, required=remember)
        encrypted = None
        if remember:
            envelope = {"profile": asdict(profile), "password": password}
            encrypted = dpapi.protect(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))
        revision = secrets.token_bytes(16)
        with self._lock:
            with self._database() as db:
                db.execute("BEGIN IMMEDIATE")
                other_count = db.execute("SELECT COUNT(*) FROM profiles WHERE id<>?", (profile.id,)).fetchone()[0]
                if other_count >= 4:
                    raise ValueError("장비는 최대 네 대까지 등록할 수 있습니다.")
                db.execute("""INSERT INTO profiles (id, host, username, fingerprint, known_hosts, revision, credential)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET host=excluded.host, username=excluded.username,
                        fingerprint=excluded.fingerprint, known_hosts=excluded.known_hosts,
                        revision=excluded.revision, credential=excluded.credential""",
                    (profile.id, profile.host, profile.username, profile.fingerprint, profile.known_hosts,
                     revision, encrypted))
                _store_default_account(db, profile.username)
            self._transient.pop(profile.id, None)
            if password and not remember:
                self._transient[profile.id] = (revision, password)

    def _credential(self, profile):
        with self._database() as db:
            return db.execute("""SELECT revision, credential FROM profiles
                WHERE id=? AND host=? AND username=? AND fingerprint=? AND known_hosts=?""",
                (profile.id, profile.host, profile.username, profile.fingerprint, profile.known_hosts)).fetchone()

    def password(self, profile):
        with self._lock:
            row = self._credential(profile)
            if row is None:
                return ""
            revision, encrypted = row
            if encrypted is None:
                cached = self._transient.get(profile.id)
                return cached[1] if cached and cached[0] == revision else ""
            try:
                data = json.loads(dpapi.unprotect(encrypted))
                if not isinstance(data, dict) or data.get("profile") != asdict(profile):
                    return ""
                password = data.get("password")
                _validate_password(password, required=True)
                return password
            except (dpapi.DPAPIError, ValueError, TypeError):
                return ""

    def remembered(self, profile):
        row = self._credential(profile)
        return row is not None and row[1] is not None

    def remove(self, id):
        with self._lock:
            with self._database() as db:
                db.execute("DELETE FROM profiles WHERE id=?", (id,))
            self._transient.pop(id, None)


class HostKeyMismatch(HostKeyUnapproved):
    """The supplied trust source disagrees with the selected target or key."""


def _initialize_host_pins(db):
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ssh_host_keys'").fetchone():
        return
    db.execute("BEGIN IMMEDIATE")
    db.execute("""CREATE TABLE IF NOT EXISTS ssh_host_keys (
        host TEXT NOT NULL, port INTEGER NOT NULL, fingerprint TEXT NOT NULL,
        PRIMARY KEY(host, port))""")
    # v0.46 profiles always used port 22. Keep verified fingerprints even if
    # the user's first action after upgrading is editing or removing a profile.
    db.execute("""INSERT OR IGNORE INTO ssh_host_keys(host, port, fingerprint)
        SELECT host, 22, fingerprint FROM profiles WHERE fingerprint <> ''""")
    db.commit()


def _remember_host_key(profile, port, fingerprint, stopped):
    # Keep endpoint pins independent of profile edits, deletion and password storage.
    endpoint = (profile.host, port)
    try:
        with Profiles()._database() as db:
            db.execute("BEGIN IMMEDIATE")
            if stopped is not None and stopped.is_set():
                raise ConnectionAbortedError("장비 연결을 중지했습니다.")
            saved = db.execute("SELECT id, username, fingerprint, known_hosts FROM profiles WHERE host=?",
                               (profile.host,)).fetchone()
            if port == 22 and saved and any(saved[2:]) and saved[2:] != (profile.fingerprint, profile.known_hosts):
                # An unsaved draft for the same endpoint must honor the stored
                # profile's trust settings, including a legacy selected file.
                keys, revoked = _configured_keys(profile.host, port, saved[2], saved[3])
                if (fingerprint in revoked or (saved[2] and saved[2] != fingerprint)
                        or (keys and all(key.fingerprint != fingerprint for key in keys))):
                    raise HostKeyMismatch("저장된 장비 설정의 SSH 키와 일치하지 않습니다.")
            existing = db.execute("SELECT fingerprint FROM ssh_host_keys WHERE host=? AND port=?",
                                  endpoint).fetchone()
            if existing is None:
                db.execute("INSERT INTO ssh_host_keys(host, port, fingerprint) VALUES(?, ?, ?)",
                           (*endpoint, fingerprint))
            elif existing[0] != fingerprint:
                # A file edit alone cannot rotate a pin. The currently saved profile
                # must explicitly authorize this exact, independently verified key.
                if profile.fingerprint != fingerprint or saved != (
                        profile.id, profile.username, fingerprint, profile.known_hosts):
                    raise HostKeyMismatch("이전에 저장한 SSH 키와 달라 인증을 중단했습니다.")
                db.execute("UPDATE ssh_host_keys SET fingerprint=? WHERE host=? AND port=?",
                           (fingerprint, *endpoint))
    except (HostKeyUnapproved, ConnectionAbortedError):
        raise
    except (OSError, RuntimeError, ValueError):
        raise HostKeyUnapproved("현재 사용자의 SSH 키 기록을 읽거나 저장할 수 없습니다.") from None


class DeviceShellUnavailable(RuntimeError):
    """The login succeeded, but the Aruba CLI channel could not be opened."""


class _FingerprintPolicy:
    def __init__(self, expected, revoked):
        self.expected = expected
        self.revoked = revoked

    def missing_host_key(self, client, hostname, key):
        # SSHClient invokes this after key exchange and before authentication.
        if key.fingerprint in self.revoked:
            raise HostKeyMismatch("해당 SSH 지문은 known_hosts에서 폐기한 키입니다.")
        if self.expected and not hmac.compare_digest(key.fingerprint, self.expected):
            raise HostKeyMismatch("장비의 SSH 키와 확인한 지문이 일치하지 않습니다.")


def _host_matches(patterns, name):
    from paramiko import HostKeys
    positive = False
    for pattern in patterns.split(","):
        negated = pattern.startswith("!")
        pattern = pattern.removeprefix("!")
        if pattern.startswith("|"):
            # Both OpenSSH v1 fields are canonical Base64 encodings of 20 bytes.
            sha1_b64 = r"[A-Za-z0-9+/]{26}[AEIMQUYcgkosw048]="
            if not re.fullmatch(rf"\|1\|{sha1_b64}\|{sha1_b64}", pattern):
                raise ValueError("invalid hashed known_hosts entry")
            matches = hmac.compare_digest(HostKeys.hash_host(name, pattern), pattern)
        else:
            # OpenSSH host patterns use * and ?. IPv6 and port brackets are literal.
            matches = fnmatchcase(name, pattern.replace("[", "[[]"))
        if matches:
            if negated:
                return False
            positive = True
    return positive


def _known_keys(path, name):
    from paramiko.hostkeys import HostKeyEntry
    trusted = []
    revoked = set()
    requires_ca = False
    with path.open(encoding="utf-8-sig") as stream:
        for line in stream:
            fields = line.split()
            if not fields or fields[0].startswith("#"):
                continue
            marker = fields.pop(0) if fields[0].startswith("@") else ""
            if not fields or fields[0].startswith("@") or (len(fields) == 1
                    and not any(c in fields[0] for c in ".:|")):
                raise ValueError("missing known_hosts target")
            if not _host_matches(fields[0], name):
                continue
            if len(fields) < 3 or marker not in ("", "@revoked", "@cert-authority"):
                raise ValueError("invalid known_hosts entry")
            if marker == "@cert-authority":
                requires_ca = True
                continue
            entry = HostKeyEntry.from_line(" ".join(fields[:3]))
            if entry is None or entry.key is None:
                raise ValueError("unsupported known_hosts entry")
            if marker == "@revoked":
                revoked.add(entry.key.fingerprint)
            else:
                trusted.append(entry.key)
    if requires_ca and not trusted:
        raise HostKeyUnapproved("이 장비의 known_hosts 항목에 인증 기관의 호스트 인증서 검증이 필요합니다.")
    return trusted, revoked


def _configured_keys(host, port, fingerprint, known_hosts):
    import paramiko
    name = host if port == 22 else f"[{host}]:{port}"
    path = Path(known_hosts) if known_hosts else Path.home() / ".ssh" / "known_hosts"
    try:
        keys, revoked = _known_keys(path, name)
    except FileNotFoundError:
        if known_hosts or path.is_symlink():
            raise HostKeyUnapproved("known_hosts 파일을 찾을 수 없습니다.") from None
        keys, revoked = [], set()
    except (OSError, ValueError, IndexError, AssertionError, paramiko.SSHException, paramiko.hostkeys.InvalidHostKey):
        raise HostKeyUnapproved("known_hosts 파일을 읽거나 확인할 수 없습니다.") from None
    if fingerprint in revoked:
        raise HostKeyMismatch("해당 SSH 지문은 known_hosts에서 폐기한 키입니다.")
    if keys:
        keys = [key for key in keys if key.fingerprint not in revoked
                and (not fingerprint or key.fingerprint == fingerprint)]
        if not keys:
            raise HostKeyMismatch("known_hosts의 유효한 키와 장비 지문을 확인하세요.")
    elif known_hosts:
        raise HostKeyUnapproved("선택한 known_hosts 파일에 이 장비의 신뢰 항목이 없습니다.")
    return keys, revoked


class _DeviceSocket(socket.socket):
    """Interrupt a pending TCP connection and subsequent SSH packets at Stop."""

    def __init__(self, host, stopped):
        super().__init__(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
        self.stopped = stopped

    def _check(self):
        if self.stopped.is_set():
            self.close()
            raise ConnectionAbortedError(errno.ECONNABORTED, "장비 연결을 중지했습니다.")

    def connect_device(self, host, port):
        self._check()
        self.setblocking(False)
        result = self.connect_ex((host, port))
        if result in (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY, errno.EINTR):
            deadline = time.monotonic() + 10
            while True:
                self._check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("SSH 연결 제한 시간이 지났습니다.")
                _, ready, errors = select.select([], [self], [self], min(.1, remaining))
                if ready or errors:
                    result = self.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                    break
        if result:
            raise OSError(result, os.strerror(result))
        self.settimeout(10)

    def send(self, data, flags=0):
        # Paramiko writes each encrypted packet here, including authentication.
        if self.stopped.is_set():
            self.close()
            raise EOFError
        return super().send(data, flags)

    def recv(self, size, flags=0):
        # The Paramiko transport polls its socket, including while waiting for a banner or PTY.
        if self.stopped.is_set():
            self.close()
            return b""
        return super().recv(size, flags)


class _DeviceSSH(PinnedSSH):
    def __init__(self, profile, port, stopped):
        super().__init__(profile.host, profile.known_hosts, port=port)
        self.profile = profile
        self.stopped = stopped
        self._device_socket = None

    def close_cli(self):
        # The cancellable socket rejects packets after Stop. Do not try to
        # send SSH channel EOF/CLOSE through it; terminate the entire local
        # transport instead. This is not a claim that a WLC command ended.
        self.close()

    def close(self):
        sock = self._device_socket
        try:
            if sock is not None:
                sock.close()
                if sock.fileno() != -1:
                    raise OSError("local SSH socket close unconfirmed")
                self._device_socket = None
        finally:
            # Even a socket close error must attempt Paramiko cleanup, and
            # must propagate so DeviceMonitor retains its owner guard.
            super().close()

    def _connect_client(self, client, username, password):
        from paramiko.auth_strategy import Password

        def verified_password():
            # Called after key exchange and known_hosts verification on this transport.
            key = client.get_transport().get_remote_server_key()
            _remember_host_key(self.profile, self.port, key.fingerprint, self.stopped)
            if self.stopped is not None and self.stopped.is_set():
                raise ConnectionAbortedError("장비 연결을 중지했습니다.")
            return password

        sock = _DeviceSocket(self.host, self.stopped) if self.stopped is not None else None
        self._device_socket = sock
        try:
            if sock is not None:
                sock.connect_device(self.host, self.port)
            client.connect(self.host, port=self.port, username=username, sock=sock,
                           timeout=10, banner_timeout=15, auth_timeout=15, channel_timeout=15,
                           auth_strategy=Password(username, verified_password))
        except BaseException:
            if sock is not None:
                sock.close()
            raise

    def _configure_trust(self, client):
        import paramiko
        fingerprint = self.profile.fingerprint
        keys, revoked = _configured_keys(self.host, self.port, fingerprint, self.known_hosts)
        if keys:
            name = self.host if self.port == 22 else f"[{self.host}]:{self.port}"
            for key in keys:
                client.get_host_keys().add(name, key.get_name(), key)
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            client.set_missing_host_key_policy(_FingerprintPolicy(fingerprint, revoked))


def open_session(profile, password, *, port=22, stopped=None):
    import paramiko
    if not isinstance(profile, DeviceProfile):
        raise ValueError("장비 설정을 확인하세요.")
    _validate_password(password, required=True)
    transport = _DeviceSSH(profile, port, stopped)
    try:
        transport.connect(profile.username, password)
    except paramiko.BadHostKeyException:
        raise HostKeyMismatch("장비의 SSH 키가 known_hosts 항목과 일치하지 않습니다.") from None
    except Exception:
        if stopped is not None and stopped.is_set():
            raise ConnectionAbortedError("장비 연결을 중지했습니다.") from None
        raise
    session = ShellSession(transport)
    try:
        session.open()
        if stopped is not None and stopped.is_set():
            raise ConnectionAbortedError
    except Exception:
        try:
            session.close()
        except (EOFError, OSError, paramiko.SSHException):
            pass
        finally:
            transport.close()
        if stopped is not None and stopped.is_set():
            raise ConnectionAbortedError("장비 연결을 중지했습니다.") from None
        raise DeviceShellUnavailable("SSH 인증에 성공했으나 장비 CLI를 열 수 없습니다.") from None
    return session
