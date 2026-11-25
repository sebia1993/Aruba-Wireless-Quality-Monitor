"""In-memory observations for the local user's native device window."""
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from threading import Event, Lock, Thread
import time

import paramiko

from .aos8 import merge_association_tables
from .collector import HostKeyUnapproved
from .device_profiles import DeviceShellUnavailable, HostKeyMismatch, open_session
from .diagnostic import COMMANDS, collect_readings


CPU, RADIO, ACTIVE, INVENTORY, ASSOCIATION, BSS = (entry[0] for entry in COMMANDS)
# Number of one-minute ticks between polls. A manual connection test collects all six.
PERIODS = {CPU: 1, RADIO: 1, ACTIVE: 2, INVENTORY: 10, ASSOCIATION: 5, BSS: 5}
OUTCOMES = {
    "parsed": "조회 성공", "permission_denied": "권한 부족", "unsupported": "명령 미지원",
    "parse_error": "응답 형식 미확인", "budget_deferred": "시간 한도로 보류",
    "transport_error": "CLI 통신 실패", "cpu_protection": "CPU 보호로 보류",
    "not_run": "미실행",
}
UNKNOWN = "미확인"


def _display(value):
    if value is None or value == "":
        return UNKNOWN
    return format(value, "g") if isinstance(value, float) else str(value)


def _time_string(epoch):
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S") if epoch is not None else "—"


def _association_counts(rows):
    # Retain only counts from this controller's complete association response.
    active, conflicts = merge_association_tables([rows])
    groups = dict(Counter((r["ap_name"], r["bssid"], r["ssid"]) for r in active.values()))
    for claims in conflicts.values():
        for r in claims:
            groups[r["ap_name"], r["bssid"], r["ssid"]] = None
    return len(active) + len(conflicts), groups


def _ap_rows(values, dates):
    inventory = values.get(INVENTORY, ())
    active = values.get(ACTIVE, ())
    ap_rows = []
    active_keys = {(a["name"], a["ip"]) for a in active}
    known = set()
    for ap in inventory:
        key = ap["name"], ap["ip"]
        known.add(key)
        listed = "활성" if key in active_keys else ("목록에 없음" if ACTIVE in values else UNKNOWN)
        ap_rows.append((ap["name"], _display(ap["ip"]), _display(ap["group"]), _display(ap["model"]),
                        _display(ap["status"]), listed, dates[INVENTORY], dates[ACTIVE]))
    for name, ip in sorted(active_keys - known):
        ap_rows.append((name, ip, UNKNOWN, UNKNOWN, UNKNOWN, "활성", "—", dates[ACTIVE]))
    return ap_rows


def _radio_rows(values, dates):
    radio_clients = defaultdict(list)
    for ap in values.get(ACTIVE, ()):
        for radio in ap["radios"]:
            key = (ap["name"], ap["ip"], radio["band"], radio["channel"])
            radio_clients[key].append(radio)
    radios = values.get(RADIO, ())
    radio_keys = [(r["name"], r["ip"], r["band"], r["channel"]) for r in radios]
    key_counts = Counter(radio_keys)
    radio_rows = []
    for radio, key in zip(radios, radio_keys):
        candidates = radio_clients.get(key, ())
        match = radio_clients.pop(key)[0] if key_counts[key] == len(candidates) == 1 else {}
        radio_rows.append(tuple(map(_display, (radio["name"], radio["band"], radio["channel"],
            radio["mode"], radio["utilization"], radio["interference"], radio["noise"], match.get("clients"),
            match.get("radio_id"), dates[RADIO], dates[ACTIVE] if match else "—"))))
    # Active details can supply radio identity, channel and count even when RF metrics fail.
    for (name, _, band, channel), radios in radio_clients.items():
        for radio in radios:
            radio_rows.append((name, band, str(channel), "활성 목록", UNKNOWN, UNKNOWN, UNKNOWN,
                               str(radio["clients"]), str(radio["radio_id"]), "—", dates[ACTIVE]))
    return radio_rows


def _bss_rows(values, dates):
    associations, by_bss = values.get(ASSOCIATION, (None, {}))
    bss_rows = []
    known_bsses = set()
    for b in values.get(BSS, ()):
        key = b["ap_name"], b["bssid"], b["ssid"]
        known_bsses.add(key)
        count = by_bss.get(key, 0) if associations is not None else None
        bss_rows.append(tuple(map(_display, (b["ap_name"], b["ssid"], b["bssid"], b["band"],
                                             b["channel"], b["clients"], count, dates[BSS], dates[ASSOCIATION]))))
    for name, bssid, ssid in sorted(by_bss.keys() - known_bsses):
        bss_rows.append((name, ssid, bssid, UNKNOWN, UNKNOWN, UNKNOWN,
                         _display(by_bss[name, bssid, ssid]), "—", dates[ASSOCIATION]))
    return bss_rows


def _tables(profile, values, times, outcomes):
    dates = {cmd: _time_string(times.get(cmd)) for cmd, _, _ in COMMANDS}
    associations, _ = values.get(ASSOCIATION, (None, {}))
    command_rows = []
    for cmd, _, _ in COMMANDS:
        outcome = outcomes.get(cmd, {})
        command_rows.append((cmd, OUTCOMES[outcome.get("status", "not_run")],
                             _display(outcome["rows"]) if outcome.get("rows") is not None else "—",
                             dates[cmd]))
    return {
        "장비": (("항목", "관측값"), [("관리 IP", profile.host), ("CPU 이용률(%)", _display(values.get(CPU))),
                ("활성 연결 수", _display(associations))]),
        "AP": (("AP 이름", "관리 IP", "그룹", "모델", "장비 보고 상태", "활성 목록",
                "인벤토리 시각", "활성 목록 시각"), _ap_rows(values, dates)),
        "Radio": (("AP 이름", "대역", "채널", "모드", "이용률(%)", "간섭(%)", "노이즈(dBm)", "연결 수",
                   "Radio ID", "Radio 시각", "연결 수 시각"), _radio_rows(values, dates)),
        "SSID / BSSID": (("AP 이름", "SSID", "BSSID", "대역", "채널", "BSS 연결 수", "활성 연결 수",
                          "BSS 시각", "활성 연결 시각"), _bss_rows(values, dates)),
        "명령 결과": (("명령", "조회 결과", "응답 행 수", "관측 시각"), command_rows),
    }


def _initial(profile=None):
    return dict(profile=profile, running=False, stale=False, observed_at=None,
                status="대기", connection="미연결", cli="미실행", tables={})


def _login_failure(exc):
    if isinstance(exc, (HostKeyMismatch, paramiko.BadHostKeyException)):
        return "SSH 키 불일치", "키 불일치 · 비밀번호 전송 전 중단. 장비 교체 여부를 확인하세요.", "미실행"
    if isinstance(exc, HostKeyUnapproved):
        return "SSH 키 미확인", "신뢰한 장비 키 확인 필요", "미실행"
    if isinstance(exc, paramiko.AuthenticationException):
        return "인증 실패", "SSH 인증 실패", "미실행"
    if isinstance(exc, DeviceShellUnavailable):
        return "CLI 열기 실패", "SSH 인증 성공 · 연결 종료", "CLI 열기 실패"
    if isinstance(exc, ValueError):
        return "설정 확인 필요", "장비 또는 계정 설정 확인 필요", "미실행"
    return "SSH 연결 실패", "SSH 연결 실패 · 주소 및 통신 상태 확인", "미실행"


@dataclass
class _Slot:
    pending: tuple | None = None
    active_host: str | None = None
    stopped: Event = field(default_factory=Event)
    session: object = None

    def cancel(self):
        self.stopped.set()
        if self.session is not None:
            cancel = getattr(self.session, "cancel", None)
            if cancel is not None:
                cancel()
        self.pending = None


class DeviceMonitor:
    """One owner per device. Failed logins and lost transports require an explicit restart."""
    def __init__(self, session_factory=open_session, interval=60):
        self._factory = session_factory
        self._interval = interval
        self._lock = Lock()
        self._slots = {}
        self._states = {}
        self._closed = False

    def configure_interval(self, seconds):
        """Apply the new base interval on subsequent polls without interrupting SSH."""
        if type(seconds) is not int or not 60 <= seconds <= 600:
            raise ValueError("수집 주기는 60~600초 사이의 정수로 입력하세요.")
        with self._lock:
            self._interval = seconds

    def start(self, profile, password, *, once=False):
        with self._lock:
            if self._closed:
                raise RuntimeError("장비 모니터가 종료되었습니다.")
            other = [slot for key, slot in self._slots.items() if key != profile.id]
            if len(other) >= 4 or any(slot.active_host == profile.host or
                    (slot.pending and slot.pending[0].host == profile.host) for slot in other):
                raise ValueError("서로 다른 관리 IP의 장비를 최대 네 대까지 조회할 수 있습니다.")
            self._states[profile.id] = dict(_initial(profile), running=True, status="연결 중", connection="연결 중")
            slot = self._slots.get(profile.id)
            if slot is not None:
                slot.cancel()
                slot.pending = (profile, password, once)
                return
            slot = _Slot(pending=(profile, password, once))
            self._slots[profile.id] = slot
            Thread(target=self._serve, args=(profile.id, slot), daemon=True, name="AWQM-device").start()

    def state(self, id):
        with self._lock:
            state = self._states.get(id, _initial()).copy()
        at = state["observed_at"]
        if at is not None and time.time() - at > 2 * self._interval + 30:
            state["stale"] = True
        return state

    def _stop_locked(self, id):
        if slot := self._slots.get(id):
            slot.cancel()
        state = self._states.get(id)
        if state and state["running"]:
            self._states[id] = dict(state, status="중지됨", running=False,
                connection="연결 중단" if state["connection"] == "연결 중" else "SSH 인증 성공 · 중지",
                stale=state["observed_at"] is not None)

    def stop(self, id):
        with self._lock:
            self._stop_locked(id)

    def forget(self, id):
        with self._lock:
            self._stop_locked(id)
            self._states.pop(id, None)

    def close(self):
        with self._lock:
            self._closed = True
            for id in self._slots:
                self._stop_locked(id)

    def _publish(self, id, stopped, **fields):
        with self._lock:
            if not stopped.is_set():
                self._states[id] = dict(self._states[id], **fields)

    def _serve(self, id, slot):
        while True:
            with self._lock:
                if slot.pending is None:
                    del self._slots[id]
                    return
                profile, password, once = slot.pending
                slot.active_host = profile.host
                slot.pending = None
                stopped = slot.stopped = Event()
            session = None
            final = {}
            try:
                if stopped.is_set():
                    continue
                session = self._factory(profile, password, stopped=stopped)
                del password
                with self._lock:
                    slot.session = session
                if not stopped.is_set():
                    self._publish(id, stopped, status="조회 중", connection="SSH 인증 성공", cli="조회 중")
                    final = self._poll(profile, session, stopped, once)
            except Exception as exc:
                if session is None:
                    status, connection, cli = _login_failure(exc)
                    final = dict(status=status, connection=connection, cli=cli)
                else:
                    final = dict(status="조회 실패", connection="SSH 인증 성공 · 연결 종료", cli="조회 처리 실패",
                                 stale=self.state(id)["observed_at"] is not None)
            finally:
                if session is not None:
                    try:
                        session.close()
                    except Exception:
                        pass
                with self._lock:
                    slot.session = None
            self._publish(id, stopped, running=False, **final)

    def _poll(self, profile, session, stopped, once):
        values, times, outcomes, due = {}, {}, {}, {}
        observed_at = None
        while not stopped.is_set():
            now = time.monotonic()
            commands = tuple(entry for entry in COMMANDS if entry[0] == CPU or (
                now >= due.get(entry[0], 0) and
                outcomes.get(entry[0], {}).get("status") not in ("permission_denied", "unsupported")))
            readings, at, attempted = collect_readings(session, commands=commands, continue_on_denied=True, stopped=stopped)
            if stopped.is_set():
                break
            if ASSOCIATION in readings:
                readings[ASSOCIATION] = _association_counts(readings[ASSOCIATION])
            for result in attempted:
                name = result["command"]
                if name == "remaining":
                    continue
                due[name] = now + PERIODS[name] * self._interval
                outcomes[name] = result
                if name not in readings and result["status"] != "transport_error":
                    values.pop(name, None)
                    times.pop(name, None)
            values.update(readings)
            times.update(at)
            cpu = readings.get(CPU)
            gated = cpu is None or cpu >= 70
            failed_transport = any(r["status"] == "transport_error" for r in attempted)
            terminal = once or failed_transport or outcomes[CPU]["status"] in ("unsupported", "permission_denied")
            if failed_transport:
                cli = "CLI 통신 실패"
            elif gated:
                reason = "이용률 70% 이상" if cpu is not None else OUTCOMES[outcomes[CPU]["status"]]
                cli = f"CPU 보호 · {reason}"
            elif any(r["status"] != "parsed" for r in outcomes.values()):
                cli = "일부 조회 · 명령 결과 확인"
            elif not any(values.get(cmd) for cmd in (INVENTORY, ACTIVE, RADIO, BSS)) and (
                    ASSOCIATION in values and values[ASSOCIATION][0] == 0):
                cli = "조회 완료 · 관측 행 없음"
            else:
                cli = "조회 완료"
            displayed = values if not gated or failed_transport else {CPU: cpu}
            visible_outcomes = outcomes.copy()
            if gated:
                for name, _, _ in COMMANDS[1:]:
                    visible_outcomes[name] = {"status": "not_run" if failed_transport else "cpu_protection"}
            elif failed_transport:
                attempted_names = {r["command"] for r in attempted}
                for name, _, _ in commands:
                    if name not in attempted_names:
                        visible_outcomes[name] = {"status": "not_run"}
            observed_at = max(at.values(), default=observed_at)
            fields = dict(status=cli, cli=cli, observed_at=observed_at,
                          stale=(failed_transport or not at) and observed_at is not None,
                          tables=_tables(profile, displayed, times, visible_outcomes))
            if terminal:
                fields["connection"] = "SSH 인증 성공 · 연결 종료"
                return fields
            self._publish(profile.id, stopped, **fields)
            stopped.wait(self._interval)
        return {}
