"""Resolve approved dashboard locations without discovering or logging into devices."""
from pathlib import Path
from urllib.parse import urlsplit

from .service_config import load_config, safe_path
from .web_config import host_name, load_web_config
from .windows_security import account_sid, check_acl


SERVICE_KEY = r"SYSTEM\CurrentControlSet\Services\AWQM\Parameters"
POLICY_KEY = r"SOFTWARE\Policies\AWQM"
VALUE = "DashboardUrl"
TRUSTED_OWNERS = {"S-1-5-18", "S-1-5-32-544",  # SYSTEM and Administrators
                  "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"}  # TrustedInstaller


def dashboard_url(value):
    if not isinstance(value, str) or not value.isascii() or len(value) > 320:
        raise ValueError("Invalid dashboard URL")
    if any(ord(char) <= 32 or ord(char) == 127 or char in "\\?#@%" for char in value):
        raise ValueError("An HTTPS origin is required")
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.netloc or parts.path not in ("", "/"):
        raise ValueError("An HTTPS origin is required")
    hostname = host_name(parts.hostname)
    port = parts.port
    if (port is not None and not 1 <= port <= 65535) or parts.netloc.endswith(":"):
        raise ValueError("Invalid dashboard port")
    return "https://" + hostname + (f":{port}" if port is not None else "") + "/"


def registry_endpoint(path):
    import winreg
    import win32security as s
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            security = s.GetSecurityInfo(int(key), s.SE_REGISTRY_KEY,
                s.OWNER_SECURITY_INFORMATION | s.DACL_SECURITY_INFORMATION)
            acl = security.GetSecurityDescriptorDacl()
            if acl is None or s.ConvertSidToStringSid(security.GetSecurityDescriptorOwner()) not in TRUSTED_OWNERS:
                raise ValueError("Unprotected dashboard setting")
            # Generic write/all, ownership, ACL, delete, create-subkey/link and set-value.
            write = 0x500D0026
            for index in range(acl.GetAceCount()):
                ace = acl.GetAce(index)
                kind, flags = ace[0]
                if kind not in (0, 1) or len(ace) != 3:
                    raise ValueError("Unverified dashboard setting permissions")
                if kind == 0 and not flags & 8 and ace[1] & write and s.ConvertSidToStringSid(ace[2]) not in TRUSTED_OWNERS:
                    raise ValueError("Writable dashboard setting")
            value, kind = winreg.QueryValueEx(key, VALUE)
    except FileNotFoundError:
        return None
    if kind != winreg.REG_SZ:
        raise ValueError("Unexpected dashboard setting type")
    return dashboard_url(value)


def verified_config_endpoint(cfg):
    # Only read a display address after establishing trust in the directory and files.
    sid = account_sid(cfg.account)
    for path in (cfg.root, safe_path(cfg.root, "config"), safe_path(cfg.root, "config/service.json")):
        check_acl(path, sid)
    path = safe_path(cfg.root, "config/web.json")
    if not path.exists():
        return None
    check_acl(path, sid)
    web = load_web_config(cfg)
    if web is None:
        return None
    for path in (safe_path(cfg.root, "secrets"), web.cert, web.key):
        check_acl(path, sid)
    return dashboard_url("https://" + web.authority)


def publish_service_endpoint(cfg):
    """Run inside service registration while the new service is still disabled."""
    import winreg
    endpoint = verified_config_endpoint(cfg)
    if endpoint:
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, SERVICE_KEY, 0,
                                winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
            winreg.SetValueEx(key, VALUE, 0, winreg.REG_SZ, endpoint)
    else:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, SERVICE_KEY, 0,
                                winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
                winreg.DeleteValue(key, VALUE)
        except FileNotFoundError:
            pass


def approved_endpoint(state):
    """A machine-wide site policy also supports operator PCs without a local service."""
    import pywintypes
    try:
        site_url = registry_endpoint(POLICY_KEY)
        if site_url or state != "running":
            return site_url
        service_url = registry_endpoint(SERVICE_KEY)
        if service_url:
            return service_url
        # Legacy registrations predate the public URL. Consult their protected
        # config only when the current account has read permission.
        from win32com.shell import shell, shellcon
        import win32service as w
        from contextlib import ExitStack
        with ExitStack() as handles:
            scm = w.OpenSCManager(None, None, w.SC_MANAGER_CONNECT)
            handles.callback(w.CloseServiceHandle, scm)
            service = w.OpenService(scm, "AWQM", w.SERVICE_QUERY_CONFIG)
            handles.callback(w.CloseServiceHandle, service)
            account = w.QueryServiceConfig(service)[7]
        root = Path(shell.SHGetFolderPath(0, shellcon.CSIDL_COMMON_APPDATA, None, 0)) / "AWQM"
        sid = account_sid(account)
        for path in (root, safe_path(root, "config"), safe_path(root, "config/service.json")):
            check_acl(path, sid)
        cfg = load_config(root)
        if account_sid(cfg.account) != sid:
            raise ValueError("Configured service account has changed")
        return verified_config_endpoint(cfg)
    except (OSError, ValueError, pywintypes.error):
        return None
