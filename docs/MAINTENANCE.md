# v0.8.0-dev maintenance

Fixture-engine schedules a closed KST day aggregate rebuild (at most one per
pass, 300-second cooldown), daily core backup, weekly core plus aggregates
backup. Backups use SQLite Online Backup, quick_check and SHA-256 manifest,
and atomic staging to a new destination. Raw backup is optional. Client/job
history, credentials and complete incident evidence are not included.
Low disk space defers maintenance. Raw data is not automatically deleted.
The fixture engine's directories are temporary; actual persistent maintenance
schedule and disk dispatch protection require service integration.

Internal `verified_backup`, `verify`, `restore_new` APIs never overwrite a
database. Restore copies only approved SQLite filenames after checksum and
integrity verification. Use only a trusted, ACL-protected backup: checksums
detect accidental corruption, not authenticity against a malicious writer.
If raw partitions are absent, affected days are retired to prevent unsafe
reconstruction from late rows. A core-only restore may block writes for
the current day; this is a conservative recovery candidate, not a
production restore workflow. Raw data, jobs, and client history are not
guaranteed recoverable. RPO/RTO and load impact remain unmeasured.

## v0.17.0-dev retention and audit backup

The protected local config/retention.json must be absent (disabled), exactly {"version":1,"enabled":false}, or exactly {"version":1,"enabled":true,"authorized":true,"policy":"balanced-7-30-365-v1"}. This opt-in schedules one bounded daily retention pass. Raw days older than seven completed KST days can be deleted only if the actual two aggregates, latest verified weekly aggregate backup (same manifest revision and count), and complete incident evidence in that backup are present. Open observed incident spans, legacy/orphan evidence, late data, missing/corrupt backups and incomplete aggregate counts defer deletion. At most seven raw days per pass. This safety policy can retain more than seven days.

5-minute (30 days) and hourly (365 days) rows are logically deleted only after raw retirement, bounded per pass. Closed incident evidence and reviews expire only after >=365 days from last observation/end/review, and if no overlapping retained raw day remains. Audit rows expire after 365 days in bounded batches. SQLite secure_delete is used for these deletes, but this is not a physical SSD erasure guarantee. Empty SQLite files, WAL/OS copies and prior backup archives may retain data; the balanced retention policy alone never expires backup archives. Scheduled archive expiration requires its own local authorization below.

New daily/weekly backups include an existing local web audit database by SQLite Online Backup, with checksums and explicit radio-core-audit manifest scope. Existing radio-core-only archives remain readable. Restored audit file is detached in the new restore directory; it is not automatically installed as the active service log. Config/credential/TLS restoration, backup location resilience, full update/rollback and large-scale maintenance latency remain outstanding. Never upload operational archives to GitHub.


## Scheduled backup archive expiration

The diagnostic service reads `config/archive-retention.json` at startup. An absent
file or exactly `{"version":1,"enabled":false}` disables archive expiration.
To enable it, an operator with access to the protected configuration chooses a
positive integer number of 24-hour days. For example, to retain archives for at
least 90 days, save this configuration and restart the service.

```json
{"version":1,"enabled":true,"authorized":true,"policy":"scheduled-archives-v1","days":90}
```

The service registers each new daily core or weekly aggregate backup created
while this policy is enabled. Earlier archives, manual copies, offline update
snapshots and unregistered directories are outside this policy. An interruption
between backup creation and registration leaves an unregistered copy. The
registration in core.db records the scheduler name, directory identity,
creation time, manifest digest and declared file checksums. Keep both the local
service state and backup directory ACL-protected.

At most once per day, maintenance considers one registered archive. Both its
scheduled time and its manifest creation time must be older than the selected
period. The current backup and the weekly backup referenced for raw retirement
are protected. Expiration requires a newer registered, checksum- and
SQLite-integrity-verified backup of the same type. The newest core and weekly
aggregate archives are retained even when both exceed the time limit.
Unrecognized files, hard links, symbolic links, junctions, other reparse points
and changed archives hold the candidate.

Maintenance records the pending operation in core.db before renaming the
archive within the backup directory. It deletes only the recorded files after
checking their hashes and removes the empty directory last. The next authorized
pass can finish an interrupted deletion. Disabling the policy pauses this
recovery. `data/maintenance.json` reports the selected archive and whether the
pass was idle, held or deleted a backup. Restore and update backups remain
separate from this rotation.
