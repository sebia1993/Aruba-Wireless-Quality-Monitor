"""Read-only SSH foundation. Not wired to the demo or approved for live use."""
from collections import deque
from dataclasses import dataclass
from threading import Lock
import re

BASE_COMMANDS=frozenset(("show version","show cpuload","show lc-cluster group-membership",
    "show lc-cluster load distribution ap","show lc-cluster load distribution client",
    "show ap radio-summary","show ap active details","show ap database long","show ap association",
    "show user-table","show user-table standby","show ap bss-table","show ap standby",
    "show memory","show datapath utilization","show ap uac-database"))
AP_COMMANDS=frozenset(("show ap debug client-table","show ap arm neighbors","show ap arm rf-summary","show ap arm history","show ap port status"))
MAC=re.compile(r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}",re.ASCII)
AP=re.compile(r"[A-Za-z0-9_.-]{1,128}",re.ASCII)

def command(name, target=None):
    if name in BASE_COMMANDS and target is None:
        return name
    if name in AP_COMMANDS and isinstance(target,str) and AP.fullmatch(target):
        return name+" ap-name "+target
    if name in ("show ap association client-mac","show ap arm neighbors bssid") and isinstance(target,str) and MAC.fullmatch(target):
        return name+" "+target
    if name=="show ap debug client-stats" and isinstance(target,str) and MAC.fullmatch(target):
        return name+" client-mac "+target+" advanced"
    raise ValueError("command or target not allowed")

@dataclass(frozen=True)
class Task:
    key: str
    cmd: str
    priority: int

class TaskQueue:
    def __init__(self, limit=20):
        self.limit=limit
        self._items=deque()
        self._running=None
        self._lock=Lock()
    def submit(self, task):
        with self._lock:
            if self._running and self._running.key==task.key or any(t.key==task.key for t in self._items):
                return "duplicate"
            if len(self._items)>=self.limit:
                return "full"
            self._items.append(task)
            return "queued"
    def next(self):
        with self._lock:
            if self._running or not self._items:
                return None
            task=min(self._items,key=lambda t:t.priority)
            self._items.remove(task)
            self._running=task
            return task
    def finish(self,key):
        with self._lock:
            if not self._running or self._running.key!=key:
                raise ValueError("task not running")
            self._running=None

class HostKeyUnapproved(RuntimeError):pass

class PinnedSSH:
    """Explicit testable transport primitive. Live CLI shell/paging/capability handling is pending."""
    def __init__(self, host, known_hosts, port=22):
        self.host,self.known_hosts,self.port=host,known_hosts,port
        self.client=None
    def _configure_trust(self, client):
        import paramiko
        try:
            client.load_host_keys(self.known_hosts)
        except (OSError,ValueError,paramiko.SSHException):
            raise HostKeyUnapproved("approved host key file unavailable or invalid") from None
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        key_name=self.host if self.port==22 else f"[{self.host}]:{self.port}"
        if client.get_host_keys().lookup(key_name) is None:
            raise HostKeyUnapproved("no approved host key")

    def _connect_client(self, client, username, password, *, sock=None):
        client.connect(self.host,port=self.port,username=username,password=password,
                       timeout=10,auth_timeout=15,banner_timeout=15,channel_timeout=15,
                       look_for_keys=False,allow_agent=False,sock=sock)

    def connect(self, username,password):
        import paramiko
        self.close()
        client=paramiko.SSHClient()
        try:
            self._configure_trust(client)
            self._connect_client(client,username,password)
            transport=client.get_transport()
            if transport is None or not transport.is_authenticated():
                raise paramiko.AuthenticationException("SSH authentication incomplete")
            transport.set_keepalive(30)
        except Exception:
            client.close()
            raise
        self.client=client
    def close(self):
        if self.client is not None:
            self.client.close()
            self.client=None
