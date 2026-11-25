# Prepare the stopped service for an offline update

This is a development feature for a local authorized Windows administrator.
No actual installation, replacement, activation or live rollback is performed
by these commands. The next program switch and schema compatibility validation
remain separate gates.

Use the approved Program Files/AWQM installation and protected ProgramData/AWQM
tree. Stop the AWQM service using the manager and check SCM STOPPED before
preparing. Do not start AWQM, rotate credentials, change the local config,
run another updater or modify the service data during preparation.
The manager verifies the registered SCM binary path/service account and holds
the same exclusive service-instance guard used before WLC/DB access.
Run `AWQM-Setup.exe /ALLUSERS` on the existing system installation to prepare the new release. Use the new external manager to prepare the snapshot, including when upgrading a v0.44 or earlier three-executable installation. Verify the setup and the service package against the approved release and internal-package digests.

```powershell
$manager = Join-Path $env:ProgramFiles 'AWQM Setup\Candidates\0.45.0\AWQM.exe'
$package = Join-Path $env:ProgramFiles 'AWQM Setup\Packages\0.45.0\AWQM_0.45.0_Win11_x64.zip'
$trustedZipSha256 = Read-Host 'Approved SHA-256 of the service ZIP'
& $manager --manager status
& $manager --manager prepare-update --id update-001 --archive $package --sha256 $trustedZipSha256
& $manager --manager verify-update --id update-001
& $manager --manager restore-update-copy --id update-001 --output-id recovery-001
```

An existing association.db requires the explicit --include-association-history
option after local privacy approval. It is not copied into normal daily/weekly
archives. The pre-update bundle stores a temporary protected rollback copy,
not a new retention authorization. Apply the original retention obligations
to any such archive and remove it by the approved local process when no longer
needed. No automatic archive deletion occurs.

The manager creates a new protected backup/updates/<id> directory containing
a verified candidate, the entire old program tree, config/secrets/data/logs
(including protected DPAPI blobs, TLS private keys and operation state), and
a complete SHA-256 file manifest. Backup archives are excluded from recursive
copy. Free space is checked; reparse points, changed inputs, incomplete
programs, nonempty SQLite WAL/rollback journals and pending spool prevent
preparation. Recover and cleanly stop the old version first if these checks fail.
All copied SQLite databases pass quick_check, and core mode/pending state is
checked. A recovery copy is verified in a new protected directory; existing
service paths are never overwritten. DPAPI remains bound to the original
account and host. Never upload these snapshots or keys to GitHub or expose
them in public release packages. ACL protection is required; cryptographic
signatures, physical erasure, off-host restore and full data switch remain
unqualified. Actual Windows SCM and service-account tests are still required.
