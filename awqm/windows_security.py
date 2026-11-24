"""Fail-closed Windows identity and conservative protected-directory checks.

The installer does not change ACLs or grant Log on as a service. The operator
must provision a dedicated non-admin account and protected directories.
"""
def account_sid(name):
    import win32security
    sid,_,kind=win32security.LookupAccountName(None,name)
    # SID type User = 1; reject group and built-in service accounts.
    value=win32security.ConvertSidToStringSid(sid)
    if kind!=1 or value in {"S-1-5-18","S-1-5-19","S-1-5-20"}:
        raise ValueError("dedicated user account required")
    return value

def current_sid():
    import win32api,win32con,win32security
    token=win32security.OpenProcessToken(win32api.GetCurrentProcess(),win32con.TOKEN_QUERY)
    try:return win32security.ConvertSidToStringSid(win32security.GetTokenInformation(token,win32security.TokenUser)[0])
    finally:token.Close()

def check_identity(account):
    expected=account_sid(account)
    if current_sid()!=expected:raise ValueError("run under designated service identity")
    import win32security
    admin=win32security.ConvertStringSidToSid("S-1-5-32-544")
    if win32security.CheckTokenMembership(None,admin):raise ValueError("service identity must not run as administrator")
    return expected

def check_acl(path,account):
    """Reject broad or unfamiliar grants, null DACLs and unexpected owners.

    Conservative check, not a complete Windows effective-permissions proof.
    Inherited ACEs must be limited to the approved owner/service/admin principals.
    """
    import win32security
    allowed={account,"S-1-5-18","S-1-5-32-544"}
    sd=win32security.GetNamedSecurityInfo(str(path),win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION|win32security.DACL_SECURITY_INFORMATION)
    owner=win32security.ConvertSidToStringSid(sd.GetSecurityDescriptorOwner())
    if owner not in allowed:raise ValueError("unexpected protected path owner")
    dacl=sd.GetSecurityDescriptorDacl()
    if dacl is None:raise ValueError("null DACL")
    for i in range(dacl.GetAceCount()):
        ace=dacl.GetAce(i)
        typ,flags=ace[0]
        if len(ace)!=3 or typ not in (0,1):raise ValueError("unverified ACL entry")
        sid=win32security.ConvertSidToStringSid(ace[2])
        if typ==0 and sid not in allowed and not (sid=="S-1-3-0" and flags&8):
            raise ValueError("unexpected ACL grant")

def check_layout(cfg,require_credentials=True):
    from .service_config import safe_path
    sid=account_sid(cfg.account)
    paths=[cfg.root,safe_path(cfg.root,"config"),safe_path(cfg.root,"secrets"),
           safe_path(cfg.root,"config/service.json"),cfg.known_hosts]
    for p in ("data","backup","logs"):
        path=safe_path(cfg.root,p)
        if not path.is_dir():raise ValueError("protected data/backup/logs directories must exist")
        paths.append(path)
    for target in cfg.targets:
        paths.append(target.credential.parent)
        if require_credentials or target.credential.exists():paths.append(target.credential)
    # Reject old files with explicit broad grants as well as directories.
    stack=[safe_path(cfg.root,name) for name in ("config","secrets","data","backup","logs")]
    checked=set(paths)
    count=0
    while stack:
        folder=stack.pop()
        for child in folder.iterdir():
            p=safe_path(cfg.root,child.relative_to(cfg.root).as_posix())
            checked.add(p);count+=1
            if count>20000:raise ValueError("protected layout exceeds preflight bound")
            if p.is_dir():stack.append(p)
    for p in checked:check_acl(p,sid)
    return sid
