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
