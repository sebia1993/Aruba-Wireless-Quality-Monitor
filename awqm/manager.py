"""Explicit local Windows service management; never exposes passwords in argv."""
import argparse,getpass,os,sys,warnings
from pathlib import Path
from uuid import uuid4
from .service_config import default_root,load_config,envelope,fixed_local
from .windows_security import account_sid,check_identity,check_layout,check_acl
from .winservice import SERVICE_NAME,DISPLAY_NAME
from .association_history import key_path,key_envelope

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
    account_sid(cfg.account);check_layout(cfg)
    # The service binary and its bundled libraries must be installed under
    # Program Files, not a mutable downloads/user-writable directory.
    if not getattr(sys,"frozen",False):raise ValueError("install using packaged AWQM_Manager.exe")
    expected=fixed_local(Path(os.environ["ProgramFiles"])/"AWQM")
    binary=fixed_local(Path(sys.executable).parent/"AWQM_Service.exe")
    if binary.parent!=expected or not binary.is_file():raise ValueError("install folder must be Program Files\\AWQM")
    password=prompt("Windows dedicated service account password: ")
    scm=w.OpenSCManager(None,None,w.SC_MANAGER_CREATE_SERVICE)
    svc=None
    try:
        svc=w.CreateService(scm,SERVICE_NAME,DISPLAY_NAME,w.SERVICE_ALL_ACCESS,
            w.SERVICE_WIN32_OWN_PROCESS,w.SERVICE_AUTO_START if auto else w.SERVICE_DEMAND_START,
            w.SERVICE_ERROR_NORMAL,'"'+str(binary)+'"',None,0,None,cfg.account,password)
        try:
            w.ChangeServiceConfig2(svc,w.SERVICE_CONFIG_DESCRIPTION,"Read-only field-unvalidated diagnostic collection")
            # Explicit finite recovery: 30s, 60s, 300s, then no further action.
            w.ChangeServiceConfig2(svc,w.SERVICE_CONFIG_FAILURE_ACTIONS,{
                "ResetPeriod":86400,"RebootMsg":"","Command":"",
                "Actions":[(w.SC_ACTION_RESTART,30000),(w.SC_ACTION_RESTART,60000),(w.SC_ACTION_RESTART,300000),(w.SC_ACTION_NONE,0)]})
            w.ChangeServiceConfig2(svc,w.SERVICE_CONFIG_FAILURE_ACTIONS_FLAG,True)
        except Exception:
            w.DeleteService(svc)
            raise
    finally:
        del password
        if svc is not None:w.CloseServiceHandle(svc)
        w.CloseServiceHandle(scm)
    print("AWQM service registered; not started. Start type:", "automatic" if auto else "manual")

def control(action):
    import win32service as w
    perms={"status":w.SERVICE_QUERY_STATUS,"start":w.SERVICE_START|w.SERVICE_QUERY_STATUS,
           "stop":w.SERVICE_STOP|w.SERVICE_QUERY_STATUS,"remove":0x00010000|w.SERVICE_QUERY_STATUS,
           "enable-auto":w.SERVICE_CHANGE_CONFIG|w.SERVICE_QUERY_STATUS}
    scm=w.OpenSCManager(None,None,w.SC_MANAGER_CONNECT)
    svc=None
    try:
        svc=w.OpenService(scm,SERVICE_NAME,perms[action])
        state=w.QueryServiceStatus(svc)[1]
        if action=="status":
            print("SCM state:",{1:"STOPPED",2:"START_PENDING",3:"STOP_PENDING",4:"RUNNING",5:"CONTINUE_PENDING",6:"PAUSE_PENDING",7:"PAUSED"}.get(state,state))
        elif action=="start":
            if state!=w.SERVICE_STOPPED:raise ValueError("service is not stopped")
            w.StartService(svc,None);print("Start requested; check status")
        elif action=="stop":
            if state!=w.SERVICE_RUNNING:raise ValueError("service is not running")
            w.ControlService(svc,w.SERVICE_CONTROL_STOP);print("Stop requested; check status")
        elif action=="enable-auto":
            w.ChangeServiceConfig(svc,w.SERVICE_NO_CHANGE,w.SERVICE_AUTO_START,w.SERVICE_NO_CHANGE,
                None,None,0,None,None,None,None)
            print("Automatic startup configured; no start request issued")
        elif action=="remove":
            if state!=w.SERVICE_STOPPED:raise ValueError("stop service before removal")
            w.DeleteService(svc);print("Service removal requested; data and credentials retained")
    finally:
        if svc is not None:w.CloseServiceHandle(svc)
        w.CloseServiceHandle(scm)

def main():
    p=argparse.ArgumentParser(description="AWQM Windows diagnostic service manager")
    sub=p.add_subparsers(dest="action",required=True)
    q=sub.add_parser("provision",help="protect WLC credential under the service account")
    q.add_argument("controller");q.add_argument("--replace",action="store_true")
    q=sub.add_parser("install",help="register using the configured dedicated account, administrator required")
    q.add_argument("--auto-start",action="store_true")
    for name in ("status","start","stop","enable-auto","remove"):
        sub.add_parser(name)
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
        elif a.action=="install":install(load_config(default_root()),a.auto_start)
        else:control(a.action)
    except Exception as exc:p.exit(2,"AWQM manager operation failed: "+type(exc).__name__+"\n")
