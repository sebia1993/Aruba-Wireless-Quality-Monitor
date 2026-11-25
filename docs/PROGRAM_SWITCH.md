# Offline program switch and pre-start reversal (v0.25)

This development path is deliberately narrower than full update/rollback.
It is not an automatic installer, migration runner or production release.
Never use it on an active service. Use a separately verified manager
package, copied to an administrator-protected local directory outside both
Program Files/AWQM and ProgramData/AWQM. The actual Windows ACL/SCM lifecycle
has not been qualified by hosted CI.

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

```powershell
AWQM_Manager.exe stop
AWQM_Manager.exe status
AWQM_Manager.exe disable-startup
AWQM_Manager.exe prepare-update --id update-001 --archive D:\Approved\candidate.zip --sha256 <trusted-digest>
AWQM_Manager.exe switch-program --id update-001 --archive D:\Approved\candidate.zip --sha256 <trusted-digest>
AWQM_Manager.exe switch-status --id update-001
AWQM_Manager.exe reverse-program --id update-001
```

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
