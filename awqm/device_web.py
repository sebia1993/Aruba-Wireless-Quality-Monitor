"""Capability-scoped current-user device settings and observations for the local browser."""
from collections import deque
from dataclasses import asdict
from threading import RLock
import time

from .device_monitor import DeviceMonitor
from .device_profiles import DeviceProfile, Profiles

SLOTS = tuple(f"WLC-{i}" for i in range(1, 5))
PAGE_SIZE = 100
TABLES = ("장비", "AP", "Radio", "SSID / BSSID", "명령 결과")
# Collection failure is not evidence of AP Down or a service outage.
FAILURES = frozenset(("SSH 키 불일치", "SSH 키 미확인", "인증 실패", "CLI 열기 실패",
                      "설정 확인 필요", "SSH 연결 실패", "조회 실패", "CLI 통신 실패"))


class DeviceWeb:
    """Share one profile store and monitor across all tabs of the desktop process."""
    def __init__(self, profiles=None, monitor=None):
        self.profiles = profiles if profiles is not None else Profiles()
        self.monitor = monitor if monitor is not None else DeviceMonitor()
        self.lock = RLock()
        self.closed = False
        self.interval = self.profiles.poll_interval()
        self.alerts_enabled = self.profiles.alerts_enabled()
        self.monitored = set()
        self.alert_conditions = {}
        self.alert_history = deque(maxlen=50)
        if monitor is None:
            self.monitor.configure_interval(self.interval)

    def close(self):
        with self.lock:
            self.closed = True
            self.monitor.close()

    def poll_alerts(self):
        """Called by the desktop timer independently of browser activity.
        
        An explicit one-shot connection test, manual stop and removal never
        trigger a monitoring alert. Do not infer AP/WLC availability.
        """
        with self.lock:
            if self.closed or not self.alerts_enabled:
                return []
            events = []
            for id in sorted(self.monitored):
                state = self.monitor.state(id)
                status = state.get("status")
                failure = status if status in FAILURES else (
                    "관측 갱신 지연" if state.get("running") and state.get("stale") else None)
                previous = self.alert_conditions.get(id)
                if failure == previous:
                    continue
                self.alert_conditions[id] = failure
                if failure is None:
                    # A new login or collection request is not proven recovery.
                    continue
                event = {"id": id, "status": failure, "at": int(time.time())}
                self.alert_history.appendleft(event)
                events.append(event)
            return events

    def _saved(self):
        return {p.id: p for p in self.profiles.list()}

    def _snapshot(self, id, table, page):
        saved = self._saved()
        entries = []
        for slot in SLOTS:
            p = saved.get(slot)
            state = self.monitor.state(slot)
            observed = state.get("profile")
            entries.append({
                "id": slot, "profile": asdict(p) if p else None,
                "remember": self.profiles.remembered(p) if p else False,
                "has_password": bool(self.profiles.password(p)) if p else False,
                "observed_host": observed.host if observed else None,
                "status": state.get("status", "대기"), "running": state.get("running", False),
                "stale": state.get("stale", False), "observed_at": state.get("observed_at"),
                "connection": state.get("connection", "미연결"), "cli": state.get("cli", "미실행"),
            })
        state = self.monitor.state(id)
        tables = state.get("tables", {})
        headers, rows = tables.get(table, ((), ()))
        start = page * PAGE_SIZE
        return {"slots": entries, "default_username": self.profiles.default_username(),
                "interval": self.interval, "alerts_enabled": self.alerts_enabled,
                "alerts": list(self.alert_history), "table": {"name": table, "headers": headers,
                "rows": rows[start:start + PAGE_SIZE], "total": len(rows), "page": page,
                "page_size": PAGE_SIZE}}

    def dispatch(self, data):
        if type(data) is not dict or not isinstance(data.get("action"), str):
            raise ValueError("요청 형식을 확인하세요.")
        action = data["action"]
        with self.lock:
            if self.closed:
                raise RuntimeError("장비 모니터가 종료되었습니다.")
            if action == "read":
                if set(data) != {"action", "id", "table", "page"}:
                    raise ValueError("조회 요청을 확인하세요.")
                id, table, page = data["id"], data["table"], data["page"]
                if id not in SLOTS or table not in TABLES or type(page) is not int or not 0 <= page <= 10000:
                    raise ValueError("조회 범위를 확인하세요.")
                return self._snapshot(id, table, page)
            if action == "interval":
                if set(data) != {"action", "seconds"}:
                    raise ValueError("수집 주기를 확인하세요.")
                seconds = data["seconds"]
                self.profiles.set_poll_interval(seconds)
                self.monitor.configure_interval(seconds)
                self.interval = seconds
                return {"ok": True}
            if action == "alerts":
                if set(data) != {"action", "enabled"} or type(data["enabled"]) is not bool:
                    raise ValueError("알림 설정을 확인하세요.")
                if data["enabled"] != self.alerts_enabled:
                    self.profiles.set_alerts_enabled(data["enabled"])
                    self.alerts_enabled = data["enabled"]
                    self.alert_conditions.clear()
                return {"ok": True}
            if action == "default_account":
                if set(data) != {"action", "username"}:
                    raise ValueError("기본 계정을 확인하세요.")
                self.profiles.set_default_username(data["username"], replace=True)
                return {"ok": True}
            id = data.get("id")
            if id not in SLOTS:
                raise ValueError("WLC 슬롯을 확인하세요.")
            if action in ("stop", "remove"):
                if set(data) != {"action", "id"}:
                    raise ValueError("장비 요청을 확인하세요.")
                self.monitored.discard(id)
                self.alert_conditions.pop(id, None)
                if action == "stop":
                    self.monitor.stop(id)
                else:
                    self.monitor.forget(id)
                    self.profiles.remove(id)
                return {"ok": True}
            if action not in ("test", "save") or set(data) != {
                "action", "id", "host", "username", "password", "remember", "fingerprint", "known_hosts"
            }:
                raise ValueError("장비 설정 요청을 확인하세요.")
            profile = DeviceProfile(id, data["host"], data["username"], data["fingerprint"], data["known_hosts"])
            password, remember = data["password"], data["remember"]
            if type(remember) is not bool or not isinstance(password, str) or len(password) > 1024:
                raise ValueError("조회 계정 비밀번호를 확인하세요.")
            saved = self._saved().get(id)
            if not password and saved == profile:
                password = self.profiles.password(profile)
            if not password:
                raise ValueError("이 장비의 조회 계정 비밀번호를 입력하세요.")
            if action == "save":
                self.profiles.save(profile, password, remember)
                if saved != profile:
                    self.monitor.forget(id)
            elif not self.profiles.default_username():
                self.profiles.set_default_username(profile.username)
            self.monitor.start(profile, password, once=action == "test")
            self.alert_conditions.pop(id, None)
            if action == "save":
                self.monitored.add(id)
            else:
                self.monitored.discard(id)
            return {"ok": True}
