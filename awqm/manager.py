"""Explicit local Windows service management; never exposes passwords in argv."""
import argparse,getpass,os,sys,warnings,json
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4
from .service_config import default_root,load_config,envelope,fixed_local
from .windows_security import account_sid,check_identity,check_layout,check_acl
from .winservice import SERVICE_NAME,DISPLAY_NAME
from .association_history import key_path,key_envelope
from .operations import configured_ops
from .windows_security import current_sid


class ServiceRegistrationError(RuntimeError):
    """Carries a fixed local operator message about service registration."""


def recovery_actions(w):
    return ((w.SC_ACTION_RESTART, 30000), (w.SC_ACTION_RESTART, 60000),
            (w.SC_ACTION_RESTART, 300000), (w.SC_ACTION_NONE, 0))


def verify_recovery(w, service):
    policy = w.QueryServiceConfig2(service, w.SERVICE_CONFIG_FAILURE_ACTIONS)
    on_error_stop = w.QueryServiceConfig2(service, w.SERVICE_CONFIG_FAILURE_ACTIONS_FLAG)
    if (not isinstance(policy, dict) or policy.get("ResetPeriod") != 86400 or
            tuple(policy.get("Actions") or ()) != recovery_actions(w) or not on_error_stop):
        raise ServiceRegistrationError("AWQM recovery policy verification failed. Review the service registration.")


def set_start_type(w, service, start_type):
    w.ChangeServiceConfig(service, w.SERVICE_NO_CHANGE, start_type, w.SERVICE_NO_CHANGE,
                          None, None, 0, None, None, None, None)

def prompt(label):
    if not sys.stdin.isatty():raise ValueError("interactive console required")
    with warnings.catch_warnings():
        warnings.simplefilter("error",getpass.GetPassWarning)
        try:value=getpass.getpass(label)
        except getpass.GetPassWarning:raise ValueError("secure console unavailable") from None
    if not value:raise ValueError("password required")
    return value

def provision(cfg,controller,replace=False):
    sid=check_identity(cfg.account);check_layout(cfg,require_credentials=False)
    target=next((t for t in cfg.targets if t.id==controller),None)
    if not target:raise ValueError("unknown configured controller")
    dst=target.credential
    if dst.exists() and not replace:raise ValueError("credential already exists; explicit --replace required")
    if replace:
        import win32service as w
        scm=w.OpenSCManager(None,None,w.SC_MANAGER_CONNECT)
        handle=None
        try:
            try:handle=w.OpenService(scm,SERVICE_NAME,w.SERVICE_QUERY_STATUS)
            except w.error as exc:
                if exc.winerror!=1060:raise
            if handle is not None and w.QueryServiceStatus(handle)[1]!=w.SERVICE_STOPPED:
                raise ValueError("stop service before rotating credentials")
        finally:
            if handle is not None:w.CloseServiceHandle(handle)
            w.CloseServiceHandle(scm)
    password=prompt("Approved WLC collection password: ")
    try:payload=envelope(target,password)
    finally:del password
    temp=dst.with_name(dst.name+".tmp-"+uuid4().hex)
    try:
        with temp.open("xb") as f:
            f.write(payload);f.flush();os.fsync(f.fileno())
        check_acl(temp,sid)
        if replace:os.replace(temp,dst)
        else:temp.rename(dst)
    finally:
        if temp.exists():temp.unlink()
    print("Credential protected under current service identity; controller:",controller)

def provision_history_key(cfg):
    sid=check_identity(cfg.account);check_layout(cfg,require_credentials=False)
    dst=key_path(cfg)
    if dst.exists():raise ValueError("history key already exists; no automatic replacement")
    temp=dst.with_name(dst.name+".tmp-"+uuid4().hex)
    try:
        with temp.open("xb") as f:
            f.write(key_envelope(cfg));f.flush();os.fsync(f.fileno())
        check_acl(temp,sid)
        temp.rename(dst)
    finally:
        if temp.exists():temp.unlink()
    print("Association history key provisioned under the current service identity")

def install(cfg,auto=False):
    import win32service as w
    from .windows_security import check_program_acl
    from .windows_update import require_admin
    from .update_snapshot import _walk
    require_admin()
    account_sid(cfg.account);check_layout(cfg)
    # The service binary and its bundled libraries must be installed under
    # Program Files, not a mutable downloads/user-writable directory.
    if not getattr(sys,"frozen",False):raise ValueError("install using packaged AWQM_Manager.exe")
    expected=fixed_local(Path(os.environ["ProgramFiles"])/"AWQM")
    binary=fixed_local(Path(sys.executable).parent/"AWQM_Service.exe")
    if binary.parent!=expected or not binary.is_file():raise ValueError("install folder must be Program Files\\AWQM")
    _walk(expected, check_program_acl)
    with ExitStack() as handles:
        scm=w.OpenSCManager(None,None,w.SC_MANAGER_CREATE_SERVICE)
        handles.callback(w.CloseServiceHandle, scm)
        password=prompt("Windows dedicated service account password: ")
        try:
            svc=w.CreateService(scm,SERVICE_NAME,DISPLAY_NAME,w.SERVICE_ALL_ACCESS,
                w.SERVICE_WIN32_OWN_PROCESS,w.SERVICE_DISABLED,
                w.SERVICE_ERROR_NORMAL,'"'+str(binary)+'"',None,0,None,cfg.account,password)
        finally:
            del password
        handles.callback(w.CloseServiceHandle, svc)
        try:
            w.ChangeServiceConfig2(svc,w.SERVICE_CONFIG_DESCRIPTION,"Read-only field-unvalidated diagnostic collection")
            w.ChangeServiceConfig2(svc,w.SERVICE_CONFIG_FAILURE_ACTIONS,{
                "ResetPeriod":86400,"RebootMsg":"","Command":"",
                "Actions":recovery_actions(w)})
            w.ChangeServiceConfig2(svc,w.SERVICE_CONFIG_FAILURE_ACTIONS_FLAG,True)
            verify_recovery(w, svc)
            set_start_type(w, svc, w.SERVICE_AUTO_START if auto else w.SERVICE_DEMAND_START)
        except BaseException:
            try:
                w.DeleteService(svc)
            except Exception:
                raise ServiceRegistrationError(
                    "AWQM installation failed and removal of its new registration could not be confirmed. "
                    "Inspect the stopped service before retrying installation.") from None
            raise
    print("AWQM service registered; not started. Start type:", "automatic" if auto else "manual")

def control(action):
    import win32service as w
    perms={"status":w.SERVICE_QUERY_STATUS,"start":w.SERVICE_START|w.SERVICE_QUERY_STATUS|w.SERVICE_QUERY_CONFIG,
           "stop":w.SERVICE_STOP|w.SERVICE_QUERY_STATUS,"remove":0x00010000|w.SERVICE_QUERY_STATUS,
           "enable-auto":w.SERVICE_CHANGE_CONFIG|w.SERVICE_QUERY_STATUS|w.SERVICE_QUERY_CONFIG,
           "disable-startup":w.SERVICE_CHANGE_CONFIG|w.SERVICE_QUERY_STATUS,
           "enable-manual":w.SERVICE_CHANGE_CONFIG|w.SERVICE_QUERY_STATUS|w.SERVICE_QUERY_CONFIG}
    scm=w.OpenSCManager(None,None,w.SC_MANAGER_CONNECT)
    svc=None
    try:
        svc=w.OpenService(scm,SERVICE_NAME,perms[action])
        state=w.QueryServiceStatus(svc)[1]
        if action=="status":
            print("SCM state:",{1:"STOPPED",2:"START_PENDING",3:"STOP_PENDING",4:"RUNNING",5:"CONTINUE_PENDING",6:"PAUSE_PENDING",7:"PAUSED"}.get(state,state))
        elif action=="start":
            if state!=w.SERVICE_STOPPED:raise ValueError("service is not stopped")
            verify_recovery(w, svc)
            w.StartService(svc,None);print("Start requested; check status")
        elif action=="stop":
            if state!=w.SERVICE_RUNNING:raise ValueError("service is not running")
            w.ControlService(svc,w.SERVICE_CONTROL_STOP);print("Stop requested; check status")
        elif action in ("enable-auto","disable-startup","enable-manual"):
            if action!="enable-auto" and state!=w.SERVICE_STOPPED:raise ValueError("stop service first")
            if action!="disable-startup":
                verify_recovery(w, svc)
            start_type={"enable-auto":w.SERVICE_AUTO_START,"disable-startup":w.SERVICE_DISABLED,
                        "enable-manual":w.SERVICE_DEMAND_START}[action]
            set_start_type(w, svc, start_type)
            print("Startup configuration changed; no start request issued")
        elif action=="remove":
            if state!=w.SERVICE_STOPPED:raise ValueError("stop service before removal")
            w.DeleteService(svc);print("Service removal requested; data and credentials retained")
    finally:
        if svc is not None:w.CloseServiceHandle(svc)
        w.CloseServiceHandle(scm)

def collection_control(cfg,action,target=None,revision=None):
    import win32security
    if not win32security.CheckTokenMembership(None,win32security.ConvertStringSidToSid("S-1-5-32-544")):
        raise ValueError("elevated local administrator required")
    check_layout(cfg,require_credentials=False)
    ops=configured_ops(cfg)
    if action=="collection-status":
        print(json.dumps({"desired":ops.snapshot(),"recent":ops.recent()},ensure_ascii=False,indent=2))
    else:
        result=ops.set("local:"+current_sid(),target,action=="collection-pause",revision)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        print("Desired state recorded. Check service-status.json for coordinator application/drain; no CLI or device change.")
def main():
    p=argparse.ArgumentParser(description="AWQM Windows diagnostic service manager")
    sub=p.add_subparsers(dest="action",required=True)
    q=sub.add_parser("provision",help="protect WLC credential under the service account")
    q.add_argument("controller");q.add_argument("--replace",action="store_true")
    q=sub.add_parser("install",help="register using the configured dedicated account, administrator required")
    q.add_argument("--auto-start",action="store_true")
    for name in ("status","start","stop","enable-auto","enable-manual","disable-startup","remove"):
        sub.add_parser(name)
    sub.add_parser("collection-status",help="show persistent desired collection state")
    for action in ("collection-pause","collection-resume"):
        q=sub.add_parser(action,help="record desired state, not immediate drain")
        q.add_argument("controller",help="configured WLC ID or all")
        q.add_argument("--revision",type=int,required=True)
    q=sub.add_parser("prepare-update",help="verify candidate and stopped-service rollback bundle; no installation")
    q.add_argument("--id",required=True);q.add_argument("--archive",type=Path,required=True)
    q.add_argument("--sha256",required=True);q.add_argument("--include-association-history",action="store_true")
    q=sub.add_parser("verify-update",help="verify local prepared bundle")
    q.add_argument("--id",required=True)
    q=sub.add_parser("restore-update-copy",help="verify rollback files into a new protected directory")
    q.add_argument("--id",required=True);q.add_argument("--output-id",required=True)
    q=sub.add_parser("switch-program",help="offline verified program-only switch; requires disabled stopped SCM")
    q.add_argument("--id",required=True);q.add_argument("--archive",type=Path,required=True);q.add_argument("--sha256",required=True)
    for action in ("switch-status","reverse-program"):
        q=sub.add_parser(action,help="inspect or reverse a pre-start program switch")
        q.add_argument("--id",required=True)
    q=sub.add_parser("rollback-data",help="stopped/disabled post-start data and program restoration; preserves displaced trees")
    q.add_argument("--id",required=True)
    q.add_argument("--acknowledge-data-displacement",action="store_true",required=True)
    q.add_argument("--include-association-history",action="store_true")
    sub.add_parser("provision-history-key",help="create a protected local history pseudonym key")
    sub.add_parser("smoke-test",help="verify imports only; no service change")
    a=p.parse_args()
    if sys.platform!="win32":p.error("Windows only")
    try:
        if a.action=="smoke-test":
            import win32service,win32security,servicemanager
            print("AWQM manager imports passed (no SCM operation)")
        elif a.action=="provision":provision(load_config(default_root()),a.controller,a.replace)
        elif a.action=="provision-history-key":provision_history_key(load_config(default_root()))
        elif a.action in ("prepare-update","verify-update","restore-update-copy","switch-program","switch-status","reverse-program","rollback-data"):
            from .windows_update import manage_update
            manage_update(load_config(default_root()),a)
        elif a.action.startswith("collection-"):collection_control(load_config(default_root()),a.action,getattr(a,"controller",None),getattr(a,"revision",None))
        elif a.action=="install":install(load_config(default_root()),a.auto_start)
        else:control(a.action)
    except ServiceRegistrationError as exc:p.exit(2,str(exc)+"\n")
    except Exception as exc:
        code = getattr(exc, "winerror", None)
        detail = f"Windows error {code}" if type(code) is int else type(exc).__name__
        p.exit(2, f"AWQM manager operation failed: {detail}\n")
