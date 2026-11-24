# Development status

## Initial implementation (WP-01 and partial WP-02/04/05/06/08)
- Typed observations, risk score, state transitions with hysteresis and missing-data handling.
- Strict normalized-fixture parser (not a validated Aruba CLI parser); synthetic snapshots only.
- Anchor selection that refuses conflicts and duplicate candidates; explicit counter epoch helper.
- Serialized task queue primitive, conservative CLI allowlist and pinned SSH host keys; no production scheduler.
- Local SQLite WAL daily raw partition and current state/incident records, idempotent replay. Full partition retention/aggregation/backup and cross-file spool recovery remain outstanding.
- Local-only synthetic web dashboard, five menus, filtering and system theme; no remote sharing, no credentials.
- Automated unit tests and Windows folder EXE build workflow. Windows Service and live auth not implemented.

## Required next work
WP-02 full scheduler, CPU/timeout/reconnect budget, persistent worker lifecycle and queue integration.
WP-03 actual Aruba parser contracts and capability validation, client/BSSID/anchor mapping, conflicts, roaming.
WP-04 durable cross-file spool, aggregation, retention, disk guard, backup/restore.
WP-05 detailed diagnostics, client activity/airtime/correlation, confidence and operator review.
WP-06 authenticated WebSocket, full details, historical replay, reports.
WP-07 Aruba identity, RBAC, HTTPS, DPAPI, audit, hardened exports.
WP-08 Windows Service, launcher/manager, update/rollback and offline checks.
WP-09 large data, security and actual 72h Windows soak.
WP-10 candidate artifacts and field guide.

No production compatibility or release readiness claims. Windows build/test status must be read from the actual Actions run. Field validation is not part of developer execution.
