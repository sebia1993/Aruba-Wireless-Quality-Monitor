# Local SQLite storage lifecycle

v0.4.0-dev: separate demo/diagnostic storage, durable core pending rows before raw partition commits, idempotent replay to latest state and transition evidence, bounded 1000-row batch and pending queue, daily KST raw partitions, monthly 5m/1h aggregates. Group by radio/epoch/band/channel/source; preserve counts, sums, extrema and valid observations. Incomplete data is not converted to zero.

Maintenance currently exposes internal APIs and is explicit, serialized, not a background production schedule. `aggregate_day` requires closed KST day and zero pending records. Revisions invalidate aggregation on late writes; prune is guarded by both aggregate manifests, real row-count checks, checkpoint and explicit evidence-ready day approval. Full evidence/PII lifecycle, automatic 7/30/365-day scheduling, monthly cleanup, backup restore, long-run performance and incident evidence remain pending.

`backup` uses SQLite online backup into a new directory, quick_check and manifest, excludes raw by default. No automatic overwrite or destructive deletion. Protect backups with organizational ACLs and encryption. Copying an active WAL database's main file is not a valid substitute.

`Store` is a single-process/single-writer contract; cross-process service exclusivity and a concurrent maintenance scheduler are not yet implemented.

Diagnostic measurements remain unvalidated: current state is UNKNOWN, quality score is unavailable and no operational incident transition is produced. Aggregate queries omit invalidated days until rebuild.
