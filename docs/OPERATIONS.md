# Collection maintenance control (v0.23)

This is collection-side control only. It does not change WLC configuration,
PoE, AP state or radio state, reboot a device, or confirm network downtime.
The authenticated admin role can record a desired pause/resume for a configured
WLC ID or all WLCs. Operator role cannot change it. CSRF, same-origin, and
revision comparison prevent untrusted/stale writes. The protected local
config/operations.db stores desired state and actor/time/target audit in
one transaction. Its scope binds the configured site and logical ID/IP mapping;
a changed mapping requires local reconciliation with the service stopped.
"all" is reserved for the complete collection scope.

The coordinator reads desired state at roughly one-second intervals (long
maintenance transactions can delay observation) and prevents new dispatch once
it applies the request. At most the already submitted worker/authentication
operation per WLC may finish; it is not forcibly interrupted. Pause becomes
applied only when that worker is idle and its SSH session is closed. Queued
pre-maintenance detail work is skipped, never replayed. Pre-maintenance
snapshots and detail results are withheld from current views; historical
committed data is retained. Resume reconnects and recollects fresh context,
staggers controllers, preserves remaining CLI budget, high-CPU latch, host-key/
credential blocks and unsupported command exclusions. No automatic resume.
Other controllers continue. Missing/corrupt state during operation stops
new dispatch. Desired versus applied revision, drain, CPU, CLI budgets,
connection status and queue counts are separate. Disk protection or service
failure can make applied status unavailable; never interpret it as drained.

Pausing the configured auth WLC also blocks new personal SSH verification.
Use an existing authorized admin session, or an elevated PowerShell session in `%ProgramFiles%\AWQM`.
```powershell
$status = .\AWQM.exe --manager collection-status | ConvertFrom-Json
.\AWQM.exe --manager collection-pause all --revision $status.desired.revision
```
After maintenance, obtain the current revision again before resuming collection:

```powershell
$status = .\AWQM.exe --manager collection-status | ConvertFrom-Json
.\AWQM.exe --manager collection-resume all --revision $status.desired.revision
```

The manager writes desired
state only and never initiates WLC SSH. Verify the protected service-status.json
or web applied status before a maintenance operation. Local ACLs must grant
the service identity write permission in the protected config directory.
Do not remove operations.db or operations.initialized; the marker prevents
accidental DB loss from creating an unpaused state. Protect both in local
configuration backup. Existing Radio backup does not include them yet.
If configured scope changes or the DB is lost, stop service and use an
authorized configuration reconciliation/restore procedure; do not bypass it
by deleting both files. Actual Windows ACL/SCM and WLC field operation remain
unverified by hosted CI. Tests use invented controllers and local HTTPS.
