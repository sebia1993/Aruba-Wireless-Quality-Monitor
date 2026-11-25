# Offline service upgrade and pre-start reversal

Run `AWQM-Setup.exe /ALLUSERS` on a host with an existing AWQM system installation. Setup stages the external manager and a versioned service archive outside the active program and data directories. This lets Windows release the active executable and libraries during the switch. Use the new external manager for v0.44 and older installations too.

1. Obtain the candidate ZIP and expected digest through the approved
   trusted release channel. Stop the old service and confirm STOPPED.
2. Use the manager's disable-startup action (no automatic start). Confirm
   the service is disabled. Do not start it or modify configuration/data
   during this procedure.
3. Prepare and verify a rollback bundle under backup/updates with the
   same update ID and trusted digest (see UPDATE_SNAPSHOT.md).
4. From the external manager, run switch-program. It re-verifies the
   original ZIP, exact old program, unchanged operational state, protected
   directories and SCM path/account/status. The independently staged
   candidate is under an admin-protected same-volume sibling. The service
   account must not have write/delete rights over the executable tree.
5. Check switch-status. The previous program tree is retained, not deleted.
   The journal records the expected bundle manifest hash and candidate
   digest. Interruption between two directory renames is recognizable.
   If either rename fails, inspect before attempting any recovery.
6. If reversal is necessary **before operational state changes**, use
   reverse-program with the same ID while SCM remains disabled/stopped.
   A changed file, missing bundle, ambiguous layout or changed operational
   state prevents reversal. The operation does not overwrite newer DBs,
   logs, credentials or PM state with older data.
7. Actual startup/upgrade compatibility, explicit schema migrations,
   full post-start data rollback and real Windows service qualification
   remain separate release gates. Do not assume this candidate can be
   started in production. No automatic startup, migration, restart or
   rollback occurs.

Use an elevated PowerShell session. Stop the running service and confirm STOPPED before disabling startup. If already stopped, begin with the status check.

```powershell
$manager = Join-Path $env:ProgramFiles 'AWQM Setup\Candidates\0.47.0\AWQM.exe'
$package = Join-Path $env:ProgramFiles 'AWQM Setup\Packages\0.47.0\AWQM_0.47.0_Win11_x64.zip'
$trustedZipSha256 = Read-Host 'Approved SHA-256 of the service ZIP'
& $manager --manager stop
& $manager --manager status
& $manager --manager disable-startup
& $manager --manager prepare-update --id update-001 --archive $package --sha256 $trustedZipSha256
& $manager --manager verify-update --id update-001
& $manager --manager switch-program --id update-001 --archive $package --sha256 $trustedZipSha256
& $manager --manager switch-status --id update-001
```

The switch updates SCM to the installed package's service command, preserving the registered identity. Once the candidate and service configuration have been checked, an authorized operator can use `& $manager --manager enable-manual` and `& $manager --manager start`. Check the protected service heartbeat. To reverse before starting the candidate or modifying its operational state, use `& $manager --manager reverse-program --id update-001` while disabled and stopped. After a candidate has written operational state, follow [post-start data rollback](DATA_ROLLBACK.md).

Use the same original candidate digest and exact prepared bundle. A
pseudonymous association history copy still requires explicit local
authorization. If the original state is changed, use the verified
non-overwriting recovery-copy procedure and an approved local recovery plan.
Program switch paths and journal are retained for local inspection. The
intermediate layout is recoverable but the two directory renames together
are not an atomic transaction; simultaneous external administrative changes,
power loss durability and actual NTFS/SCM integration still need validation.
Never run an untrusted manager or copy protected snapshots outside approved
storage. SHA-256 alone does not authenticate the publisher.
