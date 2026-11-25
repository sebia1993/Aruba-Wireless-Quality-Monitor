"""Fail-closed Windows identity and conservative protected-directory checks.

The installer does not change ACLs or grant Log on as a service. The operator
must provision a dedicated non-admin account and protected directories.
"""
def account_sid(name):
    import win32security
    if name.startswith(".\\"):
        # SCM can return a local service identity as .\user. LookupAccountName
        # requires the local computer name in place of the dot.
        import win32api
        name = win32api.GetComputerName() + name[1:]
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


def check_program_acl(path):
    """Conservative administrator-owned executable tree check.

    Read/execute grants are permitted to other principals; grants of write,
    ownership or deletion rights are not. This is not a full Windows effective
    permissions evaluator. Unknown ACE types are rejected.
    """
    import win32security
    sd=win32security.GetNamedSecurityInfo(str(path),win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION|win32security.DACL_SECURITY_INFORMATION)
    trusted={"S-1-5-18","S-1-5-32-544",current_sid()}
    try:
        sid,_,_=win32security.LookupAccountName(None,"NT SERVICE\\TrustedInstaller")
        trusted.add(win32security.ConvertSidToStringSid(sid))
    except Exception:
        # Do not trust an unknown owner on a host without this principal.
        pass
    if win32security.ConvertSidToStringSid(sd.GetSecurityDescriptorOwner()) not in trusted:
        raise ValueError("unexpected program owner")
    dacl=sd.GetSecurityDescriptorDacl()
    if dacl is None:raise ValueError("null program DACL")
    # FILE_WRITE_DATA/APPEND_DATA/WRITE_EA/DELETE_CHILD/WRITE_ATTRIBUTES,
    # DELETE/WRITE_DAC/WRITE_OWNER/GENERIC_WRITE/GENERIC_ALL/MAXIMUM_ALLOWED.
    write_mask=0x2|0x4|0x10|0x40|0x100|0x10000|0x40000|0x80000|0x40000000|0x10000000|0x02000000
    for i in range(dacl.GetAceCount()):
        ace=dacl.GetAce(i)
        if len(ace)!=3 or ace[0][0] not in (0,1):raise ValueError("unverified program ACL entry")
        typ,flags=ace[0]
        principal=win32security.ConvertSidToStringSid(ace[2])
        if typ==0 and ace[1]&write_mask and principal not in trusted:
            if principal=="S-1-3-0" and flags&8:continue
            raise ValueError("unexpected program write grant")
