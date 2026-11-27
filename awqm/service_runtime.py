"""Read-only diagnostic service with protected local configuration and optional HTTPS."""
from threading import Lock
from contextlib import contextmanager, ExitStack
from pathlib import Path
import hashlib,time,shutil,threading,sqlite3
from .web_config import load_web_config
from .field_validation import load_approvals
from .quality_ledger import QualityLedger
from .live_clients import LiveClients,load_client_approvals
from .station_validation import load_station_approvals
from .client_correlation import load_correlation_approvals
from .cluster import ClusterMonitor
from .controller_resources import load_resource_approvals
from .ap_anchors import load_anchor_approvals
from .live_ap_ports import load_port_approvals
from .live_rf_summary import load_rf_approvals
from .live_arm_history import load_arm_approvals
from .live_radio_stats import load_stats_approvals
from .retention import Retention,load_retention
from .selected_history import load_selected_history_policy
from .archive_retention import load_archive_retention
from .live_neighbors import LiveNeighbors,load_neighbor_approvals
from .association_history import AssociationHistory,load_history_approvals,load_key
from .diagnostic_view import DiagnosticView
from .manual_jobs import load_manual_policy
from .operations import configured_ops
from .diagnostic_web import create_listener
from .collector import PinnedSSH
from .collection_owner import collection_lease
from .ssh_shell import ShellSession
from .worker import ControllerWorker
from .storage import Store,disk_level
from .engine import Engine
from .maintenance import BackgroundMaintenance,Maintenance,write_json
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

@contextmanager
def service_lifetime(stop, status_path, wall):
    """Publish the terminal state after every registered resource has closed."""
    state = "failed"
    try:
        with ExitStack() as resources:
            try:
                yield resources
            finally:
                # Block new work and authentication before closing listeners and workers.
                stop.set()
        state = "stopped"
    finally:
        try:
            write_json(status_path, {"state": state, "at": int(wall()),
                                     "web_enabled": False, "field_validated": False})
        except OSError:
            if state == "stopped":
                raise
            # Keep the original failure when even its status file cannot be written.


def pump(engine,maintenance,stop,status_path,usage=shutil.disk_usage,wall=time.time,interval=.25,
         view=None,operations=None,clock=time.monotonic):
    """Run a preconstructed engine and take ownership of its shutdown."""
    with service_lifetime(stop, status_path, wall) as resources:
        resources.callback(engine.close)
        if view is not None:
            resources.callback(view.publish, {"paused": True}, available=False)
        _poll(engine,maintenance,stop,status_path,usage,wall,interval,view,operations,clock)


def _poll(engine,maintenance,stop,status_path,usage=shutil.disk_usage,wall=time.time,interval=.25,
          view=None,operations=None,clock=time.monotonic):
    last_status = last_disk = last_ops = float("-inf")
    level = "unknown"
    while not stop.is_set():
        now=clock()
        if now-last_disk>=5:
            space=usage(engine.store.root);level=disk_level(space.free,space.total);last_disk=now
        if operations is not None and now-last_ops>=1:
            try:engine.apply_operations(operations.snapshot())
            except (OSError,ValueError,RuntimeError,sqlite3.Error) as exc:
                engine.apply_operations(None,error=type(exc).__name__)
            last_ops=now
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
                             available=level!="critical" and not bool(result.get("storage_error")) and not result.get("paused"),
                             quality=getattr(engine,"field_quality",{}),
                             client_views=getattr(engine,"live_client_views",()),
                             client_enabled=bool(getattr(engine,"live_clients",None)),
                             neighbor_views=getattr(engine,"neighbor_views",{}),
                             neighbor_enabled=bool(getattr(engine,"live_neighbors",None)),
                             anchor_views=getattr(engine,"anchor_views",{}))
            write_json(status_path,{"state":"running","at":int(wall()),"disk_level":level,
                "result":result,"maintenance":getattr(engine,"last_maintenance",{}),
                "operations":result.get("operations"),
                "web_enabled":view is not None,"field_validated":False})
            last_status=now
        stop.wait(interval)

def run_service(root,stop,on_ready=None):
    cfg=load_config(root)
    check_identity(cfg.account);check_layout(cfg)
    status_path = safe_path(cfg.root,"logs/service-status.json")
    with exclusive(cfg.root), collection_lease(), service_lifetime(stop, status_path, time.time) as resources:
        web=load_web_config(cfg)
        profile=load_profile(cfg)
        manual_enabled=load_manual_policy(cfg)
        retention_enabled=load_retention(cfg)
        selected_history,selected_disclosure_days=load_selected_history_policy(cfg)
        archive_days = load_archive_retention(cfg)
        approvals=load_approvals(cfg)
        resource_approvals=load_resource_approvals(cfg,approvals)
        anchor_approvals=load_anchor_approvals(cfg,approvals)
        port_approvals=load_port_approvals(cfg,approvals)
        rf_approvals=load_rf_approvals(cfg,approvals)
        arm_approvals=load_arm_approvals(cfg,approvals)
        stats_approvals=load_stats_approvals(cfg,approvals)
        client_approvals=load_client_approvals(cfg,approvals)
        station_approvals=load_station_approvals(cfg,client_approvals)
        correlation_approvals=load_correlation_approvals(cfg,station_approvals,retention_enabled=retention_enabled)
        neighbor_approvals=load_neighbor_approvals(cfg,approvals)
        history_approvals=load_history_approvals(cfg,approvals,retention_enabled)
        history_key=load_key(cfg) if history_approvals else None
        # Preflight all blobs and known host entries before creating any session.
        import paramiko
        keys=paramiko.HostKeys(str(cfg.known_hosts))
        for t in cfg.targets:
            if keys.lookup(t.ip) is None:raise ValueError("unapproved host key")
            secret(t)
        operations=configured_ops(cfg)
        store=Store(safe_path(cfg.root,"data"),mode="diagnostic")
        gate=ConnectionGate(cfg.known_hosts,stop=stop)
        workers=[ControllerWorker(t.id,lambda t=t:gate.connect(t),mode=profile,offset=i*15,enable_bss=True,field_approval=approvals.get(t.id),live_detail=True,client_approval=client_approvals.get(t.id),station_approval=station_approvals.get(t.id),enable_cluster=True,neighbor_approval=neighbor_approvals.get(t.id),history_approval=history_approvals.get(t.id),manual_detail=manual_enabled,resource_approval=resource_approvals.get(t.id),anchor_approval=anchor_approvals.get(t.id),port_approval=port_approvals.get(t.id),rf_approval=rf_approvals.get(t.id),arm_approval=arm_approvals.get(t.id),stats_approval=stats_approvals.get(t.id)) for i,t in enumerate(cfg.targets)]
        # Always open the local history ledger so old incidents remain
        # inspectable after an attestation is revoked or expires.
        quality=QualityLedger(store)
        history=AssociationHistory(store,cfg.site,history_key,history_approvals) if history_key else None
        engine=Engine(workers,store,site=cfg.site,simulation=False,durable_jobs=False,auto=False,
                      quality_ledger=quality,live_clients=LiveClients(client_approvals,station_approvals=station_approvals,correlation_approvals=correlation_approvals) if client_approvals else None,
                      cluster_monitor=ClusterMonitor({t.id:t.ip for t in cfg.targets}),
                      live_neighbors=LiveNeighbors(neighbor_approvals,quality.incidents) if neighbor_approvals else None,association_history=history,selected_history=selected_history)
        resources.callback(engine.close)
        backup_root=safe_path(cfg.root,"backup");audit_path=safe_path(cfg.root,"logs/web-audit.db")
        maintenance=BackgroundMaintenance(Maintenance(store,backup_root,
            retention=Retention(store,backup_root,audit_path) if retention_enabled else None,
            audit_path=audit_path,archive_days=archive_days))
        resources.callback(maintenance.close)
        view=DiagnosticView(store,time.time,incidents=quality.incidents,association_history=history,
                            selected_history=engine.selected_history,
                            selected_controllers=(t.id for t in cfg.targets),
                            selected_disclosure_days=selected_disclosure_days) if web else None
        if web:
            server=create_listener(web,cfg,engine.workers,gate,view,stop,manual_enabled=manual_enabled,
                operations=operations,maintenance_status=lambda: maintenance.web_status(retention_enabled,archive_days))
            resources.callback(server.server_close)
            thread=threading.Thread(target=server.serve_forever,name="awqm-https",daemon=True)
            thread.start()
            resources.callback(thread.join)
            resources.callback(server.shutdown)
            resources.callback(view.publish, {"paused": True}, available=False)
        if on_ready is not None and not stop.is_set():
            on_ready()
        _poll(engine,maintenance,stop,status_path,view=view,operations=operations)
