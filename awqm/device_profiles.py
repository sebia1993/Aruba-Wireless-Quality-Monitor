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
        if not isinstance(self.username, str) or not USER.fullmatch(self.username):
            raise ValueError("조회 계정 이름을 확인하세요.")
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


class DeviceShellUnavailable(RuntimeError):
    """The login succeeded, but the Aruba CLI channel could not be opened."""


class _FingerprintPolicy:
    def __init__(self, expected):
        self.expected = expected

    def missing_host_key(self, client, hostname, key):
        # SSHClient invokes this after key exchange and before authentication.
        if not hmac.compare_digest(key.fingerprint, self.expected):
            raise HostKeyMismatch("장비의 SSH 키와 확인한 지문이 일치하지 않습니다.")


def _host_matches(patterns, name):
    from paramiko import HostKeys
    positive = False
    for pattern in patterns.split(","):
        negated = pattern.startswith("!")
        pattern = pattern.removeprefix("!")
        if pattern.startswith("|1|"):
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
    with path.open(encoding="utf-8-sig") as stream:
        for line in stream:
            fields = line.split()
            if not fields or fields[0].startswith("#"):
                continue
            marker = fields.pop(0) if fields[0].startswith("@") else ""
            if not fields or not _host_matches(fields[0], name) or marker == "@cert-authority":
                continue
            if marker not in ("", "@revoked") or len(fields) < 3:
                raise ValueError("invalid known_hosts entry")
            entry = HostKeyEntry.from_line(" ".join(fields[:3]))
            if entry is None or entry.key is None:
                raise ValueError("unsupported known_hosts entry")
            if marker == "@revoked":
                revoked.add(entry.key.fingerprint)
            else:
                trusted.append(entry.key)
    return trusted, revoked


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
        self.fingerprint = profile.fingerprint
        self.stopped = stopped

    def _connect_client(self, client, username, password):
        if self.stopped is None:
            return super()._connect_client(client, username, password)
        sock = _DeviceSocket(self.host, self.stopped)
        try:
            sock.connect_device(self.host, self.port)
            super()._connect_client(client, username, password, sock=sock)
        except BaseException:
            sock.close()
            raise

    def _configure_trust(self, client):
        import paramiko
        name = self.host if self.port == 22 else f"[{self.host}]:{self.port}"
        path = Path(self.known_hosts) if self.known_hosts else Path.home() / ".ssh" / "known_hosts"
        try:
            keys, revoked = _known_keys(path, name)
        except FileNotFoundError:
            if self.known_hosts:
                raise HostKeyUnapproved("선택한 known_hosts 파일을 찾을 수 없습니다.") from None
            keys, revoked = [], set()
        except (OSError, ValueError, IndexError, AssertionError, paramiko.SSHException, paramiko.hostkeys.InvalidHostKey):
            raise HostKeyUnapproved("known_hosts 파일을 읽거나 확인할 수 없습니다.") from None
        if self.fingerprint in revoked:
            raise HostKeyMismatch("해당 SSH 지문은 known_hosts에서 폐기한 키입니다.")
        if keys:
            for key in keys:
                if key.fingerprint not in revoked and (not self.fingerprint or key.fingerprint == self.fingerprint):
                    client.get_host_keys().add(name, key.get_name(), key)
            if not client.get_host_keys().lookup(name):
                raise HostKeyMismatch("known_hosts의 유효한 키와 장비 지문을 확인하세요.")
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        elif self.fingerprint and not self.known_hosts:
            client.set_missing_host_key_policy(_FingerprintPolicy(self.fingerprint))
        else:
            raise HostKeyUnapproved("이 장비의 확인된 SSH 지문 또는 known_hosts 항목이 필요합니다.")


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
