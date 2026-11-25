# Roll back service data and program after startup

Development feature, not a qualified production recovery procedure. It is
only for a candidate previously installed by switch-program with the
verified pre-update bundle and switch journal. The same external trusted
manager and stopped/disabled SCM path/account checks are mandatory.
No automatic startup, device commands or data migration.

If the candidate has modified its DB/logs, program-only reverse is blocked.
The rollback-data command can restore the saved data and logs and previous
program **only if the entire configuration and secrets trees remain
byte-identical to the prepared bundle**, except clean empty DB sidecars.
Changed operations state, credential rotations, trust keys, role mappings,
or expired/revoked approvals must not be silently reverted. Such changes
require a locally approved reconciliation/recovery procedure. Unclean
SQLite sidecars prevent initial preparation; preserve them and recover with
the appropriate original version before proceeding.

Use a trusted external frozen manager with the service stopped and startup
disabled. Verify bundle and confirm the consequences of displacement: the
previous live database/log content is restored, and observations written
since the update are no longer in the active database. Newer data/logs
are retained intact in backup/updates/<bundle-id>.data-rollback/displaced.
They are not merged with the restored database. Obtain explicit privacy
authorization for any existing association.db before copying or preserving
it. Keep the original bundle, switch journal, and all rollback folders.

Use the external manager staged by `AWQM-Setup.exe /ALLUSERS`. If the candidate service is running, stop it and check STOPPED before disabling startup.

```powershell
$manager = Join-Path $env:ProgramFiles 'AWQM Setup\Candidates\0.45.0\AWQM.exe'
& $manager --manager status
& $manager --manager disable-startup
& $manager --manager verify-update --id update-001
& $manager --manager rollback-data --id update-001 --acknowledge-data-displacement
# Add --include-association-history only with separate local authorization.
```

After restoration the manager verifies the SCM command for the restored program. A rollback to v0.44 or earlier selects its separate service executable; a unified release uses the installed AWQM.exe with the `--service` argument.

The new recovery trees are verified by exact SHA-256 file manifests and
SQLite integrity checks before moving anything. The rollback journal records
the old bundle hash and exact displaced file inventory. Every rename is
guarded and each tree's presence and content determines the next phase.
If interrupted after a rename, run the same command (same ID/authorization)
while stopped/disabled. Ambiguous layouts, incomplete preparation, changed
hashes, or configuration changes fail closed; do not delete the journal
or any directory to force progress. An interruption during preparation
before a valid journal requires local inspection rather than blind retry.
After both data/log trees are restored the existing program reversal verifies
the complete previous state and program. No operation erases newer files.
The multi-directory rename sequence is NOT atomic, and simultaneous external
administration and power-loss durability are not guaranteed.

Keep startup disabled until an authorized operator verifies the result,
current approval/credential validity, database compatibility, and dedicated
service account permissions. Existing rollback copies and displaced data
are subject to internal retention/privacy policy, not automatic deletion.
Hosted regression tests simulate rename failures and use synthetic data;
actual Windows SCM/account, NTFS behavior, and full-scale recovery timing
remain unqualified. A full config/secrets rollback is deliberately not
automatic. No actual site rollback is executed in CI.
