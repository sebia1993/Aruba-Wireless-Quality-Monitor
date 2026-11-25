# Windows diagnostic service

The diagnostic service uses the system installation of AWQM.exe. Its default
profile uses read-only safe-mode collection. A protected web configuration
enables the optional HTTPS listener. Additional local attestations enable
the corresponding quality metrics and detail collection. See
[service HTTPS setup](DIAGNOSTIC_WEB.md) and [Radio validation](FIELD_VALIDATION.md).

## Install the service host

On the Windows 11 x64 service host, an authorized administrator runs the same installer used on operator PCs:

```powershell
.\AWQM.exe /ALLUSERS
```

A fresh system installation places `AWQM.exe` and its private runtime in `%ProgramFiles%\AWQM`. The ordinary per-user installation is for viewing dashboards. If an AWQM service or a previous system installation exists, Setup stages a protected candidate under `%ProgramFiles%\AWQM Setup\Candidates\0.48.0` and its service-update ZIP and checksum under `%ProgramFiles%\AWQM Setup\Packages\0.48.0`. Follow the [offline update procedure](PROGRAM_SWITCH.md) to switch the existing installation.

## Prerequisites

Provision a dedicated
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

Use an interactive PowerShell session in `%ProgramFiles%\AWQM`. These actions apply to the system installation.

1. As the configured dedicated Windows identity, run `.\AWQM.exe --manager provision WLC-1` for each approved controller. Enter its collection password at the prompt. Use `--replace` for a separately approved credential rotation while stopped.
2. As an authorized administrator, run `.\AWQM.exe --manager install`, or choose **서비스 등록** in the app. Enter the dedicated Windows account password at the prompt. The manager registers the service disabled, configures and verifies its recovery actions, and records the approved local dashboard address if available. It then selects manual startup. Use `install --auto-start` for an approved automatic-start installation.
3. Run `.\AWQM.exe --manager start` and `.\AWQM.exe --manager status`, or choose **시작** in the app. Verify the heartbeat in the protected `logs/service-status.json`. SCM reports running after the identity, configuration, database and optional HTTPS listener checks.
4. Run `.\AWQM.exe --manager stop` or choose **중지**. Confirm STOPPED before changing configuration or credentials. Enable automatic startup after verification with `.\AWQM.exe --manager enable-auto`. To remove a stopped registration, run `.\AWQM.exe --manager remove`. Existing data and credentials remain on disk.

Windows SCM starts the installed executable with `--service`. Desktop users open the Start menu shortcut. A site administrator can publish an approved HTTPS dashboard origin to operator PCs as a REG_SZ named `DashboardUrl` under `HKLM\SOFTWARE\Policies\AWQM` (64-bit registry view). The existing authentication and TLS settings still apply. On a service host without a site policy, the app can open the running service's approved HTTPS address. [Service HTTPS configuration](DIAGNOSTIC_WEB.md)

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

The desktop, manager and service use the same installed AWQM.exe. The full Windows
workflow includes a separate [hosted SCM trial](CI_WORKFLOW.md#windows-service-lifecycle)
with a dedicated local user, fixture credentials and paused collection.
The actual target system and reboot require Windows 11 qualification.
Recovery policy is configured for 30s, 60s,
300s then no action; host key/credential rejection does not trigger
unbounded retries. One collection worker per configured WLC, serialized
new authentication, shared-account rejection blocks further attempts,
and critical disk space pauses new dispatch. Web access uses the optional approved HTTPS configuration.
Configuration/ACL verification, actual Windows 11 SCM installation,
shutdown/reboot/DPAPI identity tests, controlled WLC field checks and
long-run validation remain required.

## Windows 11 reboot qualification

Use the folder package on the intended Windows 11 x64 test PC, with the dedicated service identity and protected directories described above. Use an isolated controller fixture for this trial. Record the folder-package filename and verified ZIP SHA-256. Schedule the restart in an approved maintenance window outside the 72-hour soak.

1. Before startup, pause collection for all configured fixture controllers using the [local collection controls](OPERATIONS.md). Start the service for the initial identity and DPAPI check. Once the protected heartbeat shows all controllers paused and drained, run `.\AWQM.exe --manager enable-auto` from the installed program directory in an authorized administrator console. This verifies the recovery policy and sets automatic startup.
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
    service_exe_sha256 = (Get-FileHash -LiteralPath '.\AWQM.exe' -Algorithm SHA256).Hash
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
