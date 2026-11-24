"""Local Windows adapter for stopped-service update preparation.

Explicit program-only switching requires STOPPED and DISABLED SCM.
No automatic service start/stop or operational state overwrite.
Administrative access and an independently trusted candidate SHA are required.
"""
import json,os,re,sys
from pathlib import Path
from contextlib import contextmanager
from .service_config import safe_path,fixed_local
from .windows_security import account_sid,check_acl,check_layout,check_program_acl
from .update_snapshot import prepare,verify,restore_copy,_safe_part,_walk,_path
from .service_runtime import exclusive
from .winservice import SERVICE_NAME
from .program_switch import apply,inspect,reverse,locations

def require_admin():
    import win32security
    if not win32security.CheckTokenMembership(None,win32security.ConvertStringSidToSid("S-1-5-32-544")):
        raise ValueError("elevated local administrator required")

def update_root(cfg,create=False):
    path=safe_path(cfg.root,"backup/updates")
    if create and not path.exists():path.mkdir()
    if not path.is_dir():raise ValueError("update backup directory unavailable")
    check_acl(path,account_sid(cfg.account))
    return path

def bundle_path(cfg,ident,create=False):
    if not isinstance(ident,str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}",ident) or not _safe_part(ident):
        raise ValueError("invalid update ID")
    root=update_root(cfg,create=create)
    return safe_path(root,ident)

@contextmanager
def scm_stopped(cfg,installed,require_disabled=False):
    import win32service as w
    scm=w.OpenSCManager(None,None,w.SC_MANAGER_CONNECT)
    svc=None
    try:
        svc=w.OpenService(scm,SERVICE_NAME,w.SERVICE_QUERY_CONFIG|w.SERVICE_QUERY_STATUS)
        def guard():
            if w.QueryServiceStatus(svc)[1]!=w.SERVICE_STOPPED:
                raise RuntimeError("AWQM service must be STOPPED")
            q=w.QueryServiceConfig(svc)
            expected=str(installed/"AWQM_Service.exe")
            binary=q[3]
            if q[0]!=w.SERVICE_WIN32_OWN_PROCESS or os.path.normcase(binary) not in (os.path.normcase(expected),os.path.normcase('"'+expected+'"')):
                raise ValueError("SCM executable path or service type mismatch")
            if account_sid(q[7])!=account_sid(cfg.account):
                raise ValueError("SCM service identity mismatch")
            if q[1] not in (w.SERVICE_AUTO_START,w.SERVICE_DEMAND_START,w.SERVICE_DISABLED):
                raise ValueError("unexpected SCM start type")
            if require_disabled and q[1]!=w.SERVICE_DISABLED:
                raise RuntimeError("disable service startup before switching")
        guard()
        yield guard
    finally:
        if svc is not None:w.CloseServiceHandle(svc)
        w.CloseServiceHandle(scm)

def manage_update(cfg,args):
    require_admin()
    sid=check_layout(cfg,require_credentials=True)
    protect=lambda p:check_acl(p,sid)
    if args.action=="prepare-update":
        installed=fixed_local(Path(os.environ["ProgramFiles"])/"AWQM")
        dest=bundle_path(cfg,args.id,create=True)
        # The normal service holds this same guard before SSH and DB access.
        # Do not start AWQM or change local configuration during preparation.
        with scm_stopped(cfg,installed) as guard,exclusive(cfg.root):
            result=prepare(installed,cfg.root,args.archive,args.sha256,dest,guard,protect=protect,
                           include_association=args.include_association_history)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        print("Prepared and verified only; installed files and SCM unchanged.")
    elif args.action=="verify-update":
        m=verify(bundle_path(cfg,args.id),protect)
        print(json.dumps({"status":"verified_not_installed","old_version":m["old_version"],
            "new_version":m["new_version"],"archive_sha256":m["archive_sha256"],
            "rollback_files":len(m["old_program"]["files"])+len(m["old_state"]["files"]),
            "includes_association_history":m["include_association_history"]},ensure_ascii=False,indent=2))
    elif args.action=="restore-update-copy":
        bundle=bundle_path(cfg,args.id)
        dest=bundle_path(cfg,args.output_id)
        print(json.dumps(restore_copy(bundle,dest,protect),ensure_ascii=False,indent=2))
    elif args.action in ("switch-program","switch-status","reverse-program"):
        installed=fixed_local(Path(os.environ["ProgramFiles"])/"AWQM")
        bundle=bundle_path(cfg,args.id)
        # Windows cannot move the running frozen program tree. Run a
        # separately verified, admin-protected manager outside both trees.
        if not getattr(sys,"frozen",False):raise ValueError("packaged external manager required")
        executable=fixed_local(_path(Path(sys.executable)))
        paths=locations(installed,args.id)[:3]
        if any(p==executable or p in executable.parents for p in (*paths,cfg.root)):
            raise ValueError("run a trusted external manager outside installation and data roots")
        # Verify the external manager runtime tree as well as its EXE.
        # The operator must use a dedicated trusted package directory.
        _walk(executable.parent,check_program_acl)
        with scm_stopped(cfg,installed,require_disabled=True) as guard,exclusive(cfg.root):
            if args.action=="switch-program":
                result=apply(installed,cfg.root,bundle,args.id,args.archive,args.sha256,guard,
                             program_protect=check_program_acl,state_protect=protect)
            elif args.action=="reverse-program":
                result=reverse(installed,cfg.root,bundle,args.id,guard,
                               program_protect=check_program_acl,state_protect=protect)
            else:
                guard()
                result=inspect(installed,cfg.root,bundle,args.id,program_protect=check_program_acl,state_protect=protect)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        print("Service startup remains disabled; no automatic activation or live data restoration.")
    else:raise ValueError("unsupported update operation")
