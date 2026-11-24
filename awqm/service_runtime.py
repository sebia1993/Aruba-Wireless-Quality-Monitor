"""Continuous read-only diagnostic service runtime.

Real CLI fields remain unvalidated: diagnostic DB stores observations, never
production RF scores or client incident conclusions. No web listener, automatic
detail requests or public data export. User must authorize local trial.
"""
from threading import Lock
from contextlib import contextmanager
from pathlib import Path
import hashlib,time,shutil,threading
from .web_config import load_web_config
from .diagnostic_view import DiagnosticView
from .diagnostic_web import create_listener
from .collector import PinnedSSH
from .ssh_shell import ShellSession
from .worker import ControllerWorker
from .storage import Store,disk_level
from .engine import Engine
from .maintenance import Maintenance,write_json
from .service_config import load_config,secret,safe_path
from .windows_security import check_identity,check_layout

class SharedCredentialRejected(RuntimeError):pass
class CredentialUnavailable(RuntimeError):pass
class LoginBusy(RuntimeError):pass

class ConnectionGate:
    """Serialize new SSH authentication and suppress shared-account retries."""
    def __init__(self,known_hosts,load_secret=secret,transport_factory=PinnedSSH,shell_factory=ShellSession,stop=None):
        self.known_hosts=known_hosts;self.load_secret=load_secret
        self.transport_factory=transport_factory;self.shell_factory=shell_factory
        self.lock=Lock();self.rejected=set();self.stop=stop
    def connect(self,target):
        key=target.username.casefold()
        if not self.lock.acquire(blocking=False):raise LoginBusy("another authentication is running")
        try:
            if self.stop is not None and self.stop.is_set():raise LoginBusy("service is stopping")
            if key in self.rejected:raise SharedCredentialRejected("credential rejected earlier")
            try:password=self.load_secret(target)
            except Exception:raise CredentialUnavailable("collection credential unavailable") from None
            ssh=self.transport_factory(target.ip,str(self.known_hosts))
            try:
                ssh.connect(target.username,password)
                return self.shell_factory(ssh)
            except Exception as exc:
                ssh.close()
                if type(exc).__name__ in ("AuthenticationException","BadAuthenticationType","PasswordRequiredException"):
                    self.rejected.add(key)
                    raise SharedCredentialRejected("credential rejected") from None
                raise
            finally:del password
        finally:self.lock.release()

@contextmanager
def exclusive(root):
    """Named Windows mutex held for the lifetime of this service process."""
    import win32event,win32api,winerror
    name="Global\\AWQM-"+hashlib.sha256(str(root).casefold().encode()).hexdigest()[:32]
    h=win32event.CreateMutex(None,False,name)
    try:
        if win32api.GetLastError()==winerror.ERROR_ALREADY_EXISTS:raise RuntimeError("AWQM instance already exists")
        yield
    finally:h.Close()

def pump(engine,maintenance,stop,status_path,usage=shutil.disk_usage,wall=time.time,interval=.25,view=None):
    last_status=-1e20
    last_disk=-1e20;level="unknown"
    try:
        while not stop.is_set():
            now=wall()
            if now-last_disk>=5:
                space=usage(engine.store.root);level=disk_level(space.free,space.total);last_disk=now
            if level=="critical":
                # Do not harvest or dispatch new work while critical. Bounded
                # in-flight tasks may finish; stale samples are not synthesized.
                result={"mode":"diagnostic","field_validated":False,"paused":True,"storage_error":"disk_protection"}
            else:
                result=engine.pulse() or engine.last_result
                if not engine.error:engine.last_maintenance=maintenance.tick()
            if now-last_status>=5:
                if view is not None:
                    view.publish(result,getattr(engine,"published_radios",()),
                                 available=level!="critical" and not bool(result.get("storage_error")))
                write_json(status_path,{"state":"running","at":int(now),"disk_level":level,
                    "result":result,"maintenance":getattr(engine,"last_maintenance",{}),
                    "web_enabled":view is not None,"field_validated":False})
                last_status=now
            stop.wait(interval)
    finally:
        # Refuse new web authentication while waiting for workers to stop.
        stop.set()
        if view is not None:view.publish({"paused":True},available=False)
        engine.close()
        write_json(status_path,{"state":"stopped","at":int(wall()),"web_enabled":False,"field_validated":False})

def run_service(root,stop):
    cfg=load_config(root)
    check_identity(cfg.account);check_layout(cfg)
    web=load_web_config(cfg)
    # Preflight all blobs and known host entries before creating any session.
    import paramiko
    keys=paramiko.HostKeys(str(cfg.known_hosts))
    for t in cfg.targets:
        if keys.lookup(t.ip) is None:raise ValueError("unapproved host key")
        secret(t)
    with exclusive(cfg.root):
        store=Store(safe_path(cfg.root,"data"),mode="diagnostic")
        gate=ConnectionGate(cfg.known_hosts,stop=stop)
        workers=[ControllerWorker(t.id,lambda t=t:gate.connect(t),mode="safe",offset=i*15,enable_bss=True) for i,t in enumerate(cfg.targets)]
        engine=Engine(workers,store,site=cfg.site,simulation=False,durable_jobs=False,auto=False)
        maintenance=Maintenance(store,safe_path(cfg.root,"backup"))
        view=DiagnosticView(store,time.time) if web else None
        server=None;thread=None
        try:
            if web:
                server=create_listener(web,cfg,engine.workers,gate,view,stop)
                thread=threading.Thread(target=server.serve_forever,name="awqm-https",daemon=True)
                thread.start()
            pump(engine,maintenance,stop,safe_path(cfg.root,"logs/service-status.json"),view=view)
        finally:
            stop.set()
            if server is not None:
                if thread is not None and thread.is_alive():server.shutdown();thread.join()
                server.server_close()
            engine.close()
