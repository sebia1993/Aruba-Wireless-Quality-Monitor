# Windows diagnostic service

The diagnostic service runs on Windows from the folder package. Its default
profile uses read-only safe-mode collection. A protected web configuration
enables the optional HTTPS listener. Additional local attestations enable
the corresponding quality metrics and detail collection. See
[service HTTPS setup](DIAGNOSTIC_WEB.md) and [Radio validation](FIELD_VALIDATION.md).

## Portable distribution restriction

Run the portable executable as an ordinary user. Its service and manager entry points support import smoke tests only. For Windows service trials, use the complete folder ZIP in an administrator-protected directory. Developers can also build the folder executables from source.

```powershell
python -m pip install -r requirements-build.txt
python -m PyInstaller --noconfirm --clean --onedir --name AWQM --hidden-import win32timezone --add-data "awqm/static;awqm/static" launcher.py
Copy-Item dist\AWQM\AWQM.exe dist\AWQM\AWQM_Service.exe
Copy-Item dist\AWQM\AWQM.exe dist\AWQM\AWQM_Manager.exe
Copy-Item VERSION.txt,README.md -Destination dist\AWQM
```

Only a locally approved, administrator-protected onedir installation may be considered for the following **unqualified** service procedures. Preserve the existing dedicated account and ACL controls.

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
- As an authorized administrator, run `AWQM_Manager.exe install`. The manager
  creates a disabled service, configures its recovery actions and reads them
  back from SCM. After verification it selects manual startup, or automatic
  startup if requested with `--auto-start`. Service start is a separate step.
- AWQM_Manager.exe start; AWQM_Manager.exe status; inspect protected
  logs/service-status.json and diagnostic DB. SCM RUNNING follows the local
  identity, configuration, database and optional listener startup checks.
  The protected status file records a clean shutdown as stopped and a
  runtime or cleanup failure as failed. Use SCM and the Windows event log
  for failures before the protected status directory is available.
- AWQM_Manager.exe stop and wait until STOPPED before changing credentials
  or configuration. Only after local verification use
  AWQM_Manager.exe enable-auto (or install --auto-start on an approved
  fresh installation).
- AWQM_Manager.exe remove only when stopped; no data deletion.

Installation verifies a 24-hour failure-count reset, restart delays of
30, 60 and 300 seconds followed by no action, and recovery on a reported
error stop. The manager also checks this policy before starting the service
or enabling manual or automatic startup. Status, stop, disable and removal
remain available if the policy needs repair. A failed install requests
removal only for the registration created by that invocation. If the
manager was terminated or reports a cleanup failure, inspect the service’s
binary path, account, state and startup setting in Windows Services. Once
you confirm a stopped, incomplete AWQM registration, remove it before a
fresh install. The manager preserves an existing registration on a name
collision.

Service and manager entry points share the bundled runtime. The full folder
workflow includes a separate [hosted SCM trial](CI_WORKFLOW.md#windows-service-lifecycle)
with a dedicated local user, fixture credentials and paused collection.
The actual target system and reboot require Windows 11 qualification.
Recovery policy is configured for 30s, 60s,
300s then no action; host key/credential rejection does not trigger
unbounded retries. One collection worker per configured WLC, serialized
new authentication, shared-account rejection blocks further attempts,
and critical disk space pauses new dispatch. No production web access.
Configuration/ACL verification, actual Windows 11 SCM installation,
shutdown/reboot/DPAPI identity tests, controlled WLC field checks and
long-run validation remain required.

## Windows 11 reboot qualification

Use the folder package on the intended Windows 11 x64 test PC, with the dedicated service identity and protected directories described above. Use an isolated controller fixture for this trial. Record the folder-package filename and verified ZIP SHA-256. Schedule the restart in an approved maintenance window outside the 72-hour soak.

1. Before startup, pause collection for all configured fixture controllers using the [local collection controls](OPERATIONS.md). Start the service for the initial identity and DPAPI check. Once the protected heartbeat shows all controllers paused and drained, run `.\AWQM_Manager.exe enable-auto` from the installed program directory in an authorized administrator console. This verifies the recovery policy and sets automatic startup.
2. Run the observation block below in PowerShell from the installed program directory. Save its output locally as the **before** record. Confirm Windows client build 22000 or later on x64, `startup` equal to `Auto`, `scm` equal to `Running`, `heartbeat` equal to `running` and `fixture_paused` true.
3. Have the authorized operator restart Windows through the site's normal procedure. After sign-in, leave AWQM under automatic startup. Run the same observation block twice, at least five seconds apart, as the **after** records. If SCM is still starting, wait for the service to reach readiness before collecting the pair. A manual service start makes the automatic-start result inconclusive.

```powershell
$ErrorActionPreference = 'Stop'
$os = Get-CimInstance Win32_OperatingSystem
$service = Get-CimInstance Win32_Service -Filter "Name='AWQM'"
$statusFile = Join-Path $env:ProgramData 'AWQM\logs\service-status.json'
$status = Get-Content -LiteralPath $statusFile -Raw | ConvertFrom-Json
$controllers = @($status.operations.controllers | Where-Object { $_ })
$active = @($controllers | Where-Object { $_.phase -ne 'paused' -or $_.inflight -ne $false })
[pscustomobject]@{
    windows_build = $os.BuildNumber
    client_os = ($os.ProductType -eq 1)
    x64 = (@((Get-CimInstance Win32_Processor).Architecture) -contains 9)
    boot_unix = ([DateTimeOffset]$os.LastBootUpTime).ToUnixTimeSeconds()
    observed_unix = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    service_exe_sha256 = (Get-FileHash -LiteralPath '.\AWQM_Service.exe' -Algorithm SHA256).Hash
    startup = $service.StartMode
    scm = $service.State
    heartbeat = $status.state
    heartbeat_unix = $status.at
    fixture_controllers = $controllers.Count
    fixture_paused = ($controllers.Count -gt 0 -and $active.Count -eq 0)
} | ConvertTo-Json
```

Pass when the after records show a newer `boot_unix`, the same service executable hash, automatic startup and SCM running, and the expected number of paused fixture controllers. Both heartbeats must show `running` with `boot_unix <= heartbeat_unix <= observed_unix`. The heartbeat timestamp must advance between the two samples. Startup checks the process identity against the configured account and decrypts every configured controller credential before reporting readiness. This exercises service-account DPAPI without publishing an account name or credential.

Return the package filename and digest, the three observations, whether any manual start occurred, and the outcome. Record missing, stale or failed observations as such. Inspect detailed status and Windows service events locally if diagnosis is needed. The fields above provide the evidence without copying the full protected status, config, event logs or DPAPI blobs.
