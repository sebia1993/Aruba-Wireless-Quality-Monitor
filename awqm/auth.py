"""Existing Aruba identity authorization, session lifecycle and bounded login.

No web passwords are persisted. A configured, pinned controller is used solely
to verify the supplied credentials, not to open a CLI. Local role mapping is
authorization, not a separate account database. No fallback after a login
attempt; production central AAA integration remains a separate approval.
"""
from collections import defaultdict,deque
from dataclasses import dataclass
from threading import RLock,Lock
import hashlib,hmac,ipaddress,re,secrets,time
from pathlib import Path
from .collector import PinnedSSH

ROLES={"operator","admin"}
ID=re.compile(r"[A-Za-z0-9_.@\\-]{1,128}",re.ASCII)
class AuthDenied(Exception):pass
class AuthBusy(Exception):pass

def validate_roles(mapping):
    if not isinstance(mapping,dict) or not 1<=len(mapping)<=1000:
        raise ValueError("approved role mapping required")
    if any(not isinstance(k,str) or not ID.fullmatch(k) or v not in ROLES for k,v in mapping.items()):
        raise ValueError("invalid identity or role")
    if len({k.casefold() for k in mapping})!=len(mapping):
        raise ValueError("ambiguous identities")
    return dict(mapping)

class ArubaVerifier:
    def __init__(self,host,known_hosts,transport_factory=PinnedSSH):
        self.host=str(ipaddress.ip_address(host))
        self.known_hosts=Path(known_hosts)
        if not self.known_hosts.is_file():raise ValueError("trusted known_hosts required")
        self.factory=transport_factory
    def __call__(self,username,password):
        # Only the configured address may receive credentials.
        ssh=self.factory(self.host,str(self.known_hosts.resolve()))
        try:
            ssh.connect(username,password)
            transport=ssh.client.get_transport()
            return bool(transport and transport.is_authenticated())
        finally:ssh.close()

@dataclass
class Session:
    username:str
    role:str
    created:float
    touched:float
    csrf:str

class Auth:
    def __init__(self,roles,verifier,clock=time.monotonic,idle=1800,absolute=28800,
                 global_limit=10,user_limit=3,window=60,max_sessions=100,audit_sink=None):
        self.roles=validate_roles(roles);self.verifier=verifier;self.clock=clock
        self.idle=idle;self.absolute=absolute;self.global_limit=global_limit;self.user_limit=user_limit
        self.window=window;self.max_sessions=max_sessions
        self.lock=RLock();self.verify_lock=Lock()
        self.global_attempts=deque();self.user_attempts={}
        self.sessions={};self.audit=deque(maxlen=1000);self.audit_sink=audit_sink
    def _key(self,token):
        return hashlib.sha256(token.encode("ascii")).hexdigest()
    def _event(self,user,event):
        # No password, token, CLI output or exception text.
        entry={"username":user if user in self.roles else None,
               "event":event,"at":int(time.time())}
        if self.audit_sink:self.audit_sink(entry)
        self.audit.append(entry)
    def _prune(self,now):
        for k,s in list(self.sessions.items()):
            if now-s.created>=self.absolute or now-s.touched>=self.idle or self.roles.get(s.username)!=s.role:
                del self.sessions[k]
        while self.global_attempts and now-self.global_attempts[0]>=self.window:self.global_attempts.popleft()
        for k,q in list(self.user_attempts.items()):
            while q and now-q[0]>=self.window:q.popleft()
            if not q:del self.user_attempts[k]
    def login(self,user,password):
        if not isinstance(user,str) or not ID.fullmatch(user) or not isinstance(password,str) or not 1<=len(password)<=1024:
            raise AuthDenied("authentication failed")
        now=self.clock()
        with self.lock:
            self._prune(now)
            if len(self.global_attempts)>=self.global_limit:raise AuthBusy("login temporarily limited")
            self.global_attempts.append(now)
            key=user.casefold()
            q=self.user_attempts.setdefault(key,deque())
            if len(q)>=self.user_limit:raise AuthBusy("login temporarily limited")
            q.append(now)
            role=self.roles.get(user)
            if role is None:
                self._event(user,"denied");raise AuthDenied("authentication failed")
            if len(self.sessions)>=self.max_sessions:raise AuthBusy("session capacity reached")
        if not self.verify_lock.acquire(blocking=False):raise AuthBusy("authentication busy")
        try:
            try:ok=self.verifier(user,password) is True
            except AuthBusy:raise
            except Exception:ok=False
        finally:self.verify_lock.release()
        with self.lock:
            now=self.clock();self._prune(now)
            if not ok or self.roles.get(user)!=role:
                self._event(user,"denied");raise AuthDenied("authentication failed")
            if len(self.sessions)>=self.max_sessions:raise AuthBusy("session capacity reached")
            token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32)
            self._event(user,"login")
            self.sessions[self._key(token)]=Session(user,role,now,now,csrf)
            return token
    def session(self,token,role=None,touch=True):
        if not isinstance(token,str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}",token):raise AuthDenied("session required")
        with self.lock:
            now=self.clock();self._prune(now)
            s=self.sessions.get(self._key(token))
            if not s or role is not None and s.role!=role:raise AuthDenied("access denied")
            if touch:s.touched=now
            return Session(s.username,s.role,s.created,s.touched,s.csrf)
    def logout(self,token,csrf):
        with self.lock:
            s=self.session(token,touch=False)
            if not isinstance(csrf,str) or not hmac.compare_digest(s.csrf,csrf):raise AuthDenied("invalid csrf")
            self.sessions.pop(self._key(token),None);self._event(s.username,"logout")
    def set_roles(self,mapping):
        with self.lock:
            self.roles=validate_roles(mapping)
            self._prune(self.clock())
