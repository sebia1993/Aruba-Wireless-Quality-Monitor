# Offline update preparation

The single-EXE portable ZIP is not a service-installation candidate. Its GitHub release provides a SHA-256 checksum separately. The existing staging and rollback verifier deliberately accepts only the legacy complete onedir service package, not the portable ZIP. This prevents treating a portable preview as a qualified service update.

Obtain the ZIP and its expected SHA-256 through an independently trusted release channel. SHA-256 alone does not authenticate the publisher; the development artifact is not a qualified release.

```powershell
AWQM.exe --stage-package AWQM_0.20.0-dev_Win11_x64.zip --sha256 <trusted-64-hex-digest> --output C:\AWQM-Updates\v020-staged
```
The parent must already exist and be ACL-protected. The destination must not exist. This command verifies the exact copied archive, checks Windows path collisions and traversal/symlinks, bounds unpacked size, checks required executable/runtime/docs files and writes a staging receipt. It never executes binaries, changes the installed folder, service, registry, config, DB or secrets.

Before an actual upgrade, a local authorized administrator must verify an offline, complete rollback copy of the old program and the corresponding configuration/schema-compatible database, credential/key recovery and restore, stop the service safely, and test startup and rollback under the dedicated identity. Actual SCM-based atomic switch and rollback are still outstanding; no automatic installation or rollback is claimed.
