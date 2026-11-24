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
from .field_validation import load_approvals
from .quality_ledger import QualityLedger
from .live_clients import LiveClients,load_client_approvals
from .station_validation import load_station_approvals
from .cluster import ClusterMonitor
from .retention import Retention,load_retention
from .live_neighbors import LiveNeighbors,load_neighbor_approvals
from .association_history import AssociationHistory,load_history_approvals,load_key
from .diagnostic_view import DiagnosticView
from .diagnostic_web import create_listener
from .collector import PinnedSSH
from .ssh_shell import ShellSession
from .worker import ControllerWorker
from .storage import Store,disk_level
from .engine import Engine
from .maintenance import Maintenance,write_json
from .service_config import load_config,secret,safe_path,load_profile
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
                                 available=level!="critical" and not bool(result.get("storage_error")),
                                 quality=getattr(engine,"field_quality",{}),
                                 client_views=getattr(engine,"live_client_views",()),
                                 client_enabled=bool(getattr(engine,"live_clients",None)),
                                 neighbor_views=getattr(engine,"neighbor_views",{}),
                                 neighbor_enabled=bool(getattr(engine,"live_neighbors",None)))
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
    profile=load_profile(cfg)
    retention_enabled=load_retention(cfg)
    approvals=load_approvals(cfg)
    client_approvals=load_client_approvals(cfg,approvals)
    station_approvals=load_station_approvals(cfg,client_approvals)
    neighbor_approvals=load_neighbor_approvals(cfg,approvals)
    history_approvals=load_history_approvals(cfg,approvals,retention_enabled)
    history_key=load_key(cfg) if history_approvals else None
    # Preflight all blobs and known host entries before creating any session.
    import paramiko
    keys=paramiko.HostKeys(str(cfg.known_hosts))
    for t in cfg.targets:
        if keys.lookup(t.ip) is None:raise ValueError("unapproved host key")
        secret(t)
    with exclusive(cfg.root):
        store=Store(safe_path(cfg.root,"data"),mode="diagnostic")
        gate=ConnectionGate(cfg.known_hosts,stop=stop)
        workers=[ControllerWorker(t.id,lambda t=t:gate.connect(t),mode=profile,offset=i*15,enable_bss=True,field_approval=approvals.get(t.id),live_detail=True,client_approval=client_approvals.get(t.id),station_approval=station_approvals.get(t.id),enable_cluster=True,neighbor_approval=neighbor_approvals.get(t.id),history_approval=history_approvals.get(t.id)) for i,t in enumerate(cfg.targets)]
        # Always open the local history ledger so old incidents remain
        # inspectable after an attestation is revoked or expires.
        quality=QualityLedger(store)
        history=AssociationHistory(store,cfg.site,history_key,history_approvals) if history_key else None
        engine=Engine(workers,store,site=cfg.site,simulation=False,durable_jobs=False,auto=False,
                      quality_ledger=quality,live_clients=LiveClients(client_approvals,station_approvals=station_approvals) if client_approvals else None,
                      cluster_monitor=ClusterMonitor({t.id:t.ip for t in cfg.targets}),
                      live_neighbors=LiveNeighbors(neighbor_approvals,quality.incidents) if neighbor_approvals else None,association_history=history)
        backup_root=safe_path(cfg.root,"backup");audit_path=safe_path(cfg.root,"logs/web-audit.db")
        maintenance=Maintenance(store,backup_root,
            retention=Retention(store,backup_root,audit_path) if retention_enabled else None,audit_path=audit_path)
        view=DiagnosticView(store,time.time,incidents=quality.incidents,association_history=history) if web else None
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
