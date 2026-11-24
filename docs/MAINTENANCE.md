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

5-minute (30 days) and hourly (365 days) rows are logically deleted only after raw retirement, bounded per pass. Closed incident evidence and reviews expire only after >=365 days from last observation/end/review, and if no overlapping retained raw day remains. Audit rows expire after 365 days in bounded batches. SQLite secure_delete is used for these deletes, but this is not a physical SSD erasure guarantee. Empty SQLite files, WAL/OS copies and prior backup archives may retain data; archive rotation and complete lifecycle verification remain outstanding. No automatic archive removal is performed.

New daily/weekly backups include an existing local web audit database by SQLite Online Backup, with checksums and explicit radio-core-audit manifest scope. Existing radio-core-only archives remain readable. Restored audit file is detached in the new restore directory; it is not automatically installed as the active service log. Config/credential/TLS restoration, backup location resilience, full update/rollback and large-scale maintenance latency remain outstanding. Never upload operational archives to GitHub.
