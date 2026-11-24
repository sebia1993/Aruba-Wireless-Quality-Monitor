# Windows diagnostic service (v0.9.0-dev)

This is development software. The service connects to configured WLCs only
when the operator installs and starts it locally. It runs read-only
safe-mode basic collection, stores unvalidated observations in a diagnostic
DB, and never starts a web listener or enables automatic detail collection.
Do not deploy as a verified production system.

## Prerequisites

Install the complete ZIP folder as C:/Program Files/AWQM with normal
administrator-protected executable/library ACLs. Provision a dedicated
non-admin Windows account, service logon right and DPAPI profile using your
organization's approved process. The manager does not grant privileges or
modify firewall/ACL policies. Under C:/ProgramData/AWQM precreate config,
secrets, data, backup and logs directories. Set owner and ACL to SYSTEM,
Administrators and the designated service SID only; remove broad inherited
grants as approved. Unknown ACEs, null DACLs, unexpected owners, network
paths and reparse points fail closed. Actual policy and effective permissions
must be verified locally.

Create config/service.json (local only, example TEST-NET addresses):

```json
{
 "version": 1,
 "mode": "diagnostic",
 "authorized": true,
 "windows_account": "DOMAIN\\awqm-service",
 "site": "local-trial",
 "known_hosts": "secrets/known_hosts",
 "controllers": [
   {"id":"WLC-1","ip":"192.0.2.10","username":"approved-readonly","credential":"secrets/wlc1.dpapi"}
 ]
}
```

Supply trusted OpenSSH known_hosts after independent host key verification.
Do not commit config, host keys, blobs, certificates, DBs or logs. Device
credential files are controller/IP/username-bound and encrypted under the
same designated Windows identity that runs the service.

## Local procedures

- Under the designated service identity, in an interactive console:
  AWQM_Manager.exe provision WLC-1 (repeat for configured devices; explicit
  --replace for rotation). Password is prompted, not passed in argv.
- As authorized administrator: AWQM_Manager.exe install. Initial
  registration is manual startup for safe verification; no start occurs.
- AWQM_Manager.exe start; AWQM_Manager.exe status; inspect protected
  logs/service-status.json and diagnostic DB. Service status is not
  evidence of RF data validity.
- AWQM_Manager.exe stop and wait until STOPPED before changing credentials
  or configuration. Only after local verification use
  AWQM_Manager.exe enable-auto (or install --auto-start on an approved
  fresh installation).
- AWQM_Manager.exe remove only when stopped; no data deletion.

Service and manager entry points share the bundled runtime. Windows CI
smoke checks import/packaging only, not installation, boot, SCM recovery,
or actual account profile. Recovery policy is configured for 30s, 60s,
300s then no action; host key/credential rejection does not trigger
unbounded retries. One collection worker per configured WLC, serialized
new authentication, shared-account rejection blocks further attempts,
and critical disk space pauses new dispatch. No production web access.
Configuration/ACL verification, actual Windows 11 SCM installation,
shutdown/reboot/DPAPI identity tests, controlled WLC field checks and
long-run validation remain required.
