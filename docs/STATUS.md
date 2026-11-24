# Development status — second increment

- WP-01: sanitized approved design baseline, synthetic test contracts, initial build workflow. Previous Windows v0.1.0-dev build, frozen executable smoke test and ZIP succeeded.
- WP-02 partial: persistent pinned-host SSH interactive shell with deadlines, serialized CLI, bounded output, known pager and strict prompt checks; clock-injected scheduler with per-controller single command, CPU protection, budget and missed-slot handling. Dependency-injected controller worker, backoff, permanent capability/permission errors, freshness and independent partial coverage. Not connected to live service; remaining commands, adaptive detailed queue, fair scheduling and orchestration pending.
- WP-03 partial: documentation-shaped parsers for radio summary, AP inventory, active details, association and CPU. Offline probe available in launcher and packaged EXE. Physical Radio/AAC/MAC mapping, active client dedup, stale/ambiguous rejection. Conservative coherent snapshots are a development contract; production needs individual field freshness and targeted refresh of slow inventory/anchor polls. Actual firmware output, remaining CLI parsers, UAC/BSSID and roaming integration pending.
- WP-04 partial: SQLite WAL daily raw partition and current state/transition records, idempotent replay. Full spool, retention, aggregation, backup pending.
- WP-05 partial: Radio quality/hysteresis, client score/impact primitives. Real detailed client/Airtime and evidence correlation pending.
- WP-06 partial: synthetic localhost dashboard only; user analysis, production HTTP/WS and reports pending.
- WP-07 pending: Aruba identity, RBAC, HTTPS, DPAPI, audit, hardened exports.
- WP-08 partial: Windows folder EXE build pipeline, service/manager/update pending.
- WP-09/10 pending: comprehensive integration, security, 72h Windows soak and field-validation candidate.

Current launcher is synthetic-only or offline parser validation. This is not production software. Actual device tests are user-owned. Build status for this increment must be verified in Actions.
