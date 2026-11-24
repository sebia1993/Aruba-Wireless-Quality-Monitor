# Collection engine (v0.5.0-dev)

Internal Engine coordinator, injected controller sessions, up to four independent worker futures, one serialized SQLite persistence path. An SSH worker may be slow while other workers continue. Collection dispatch pauses after a storage failure; pending writes can be recovered explicitly. Diagnostic storage never generates verified incidents or operational scores.

Manual jobs: immutable receipt ID and request ID, duplicate target merging, 20 active per controller, limited in-memory history, deadline, queued/deferred/running/completed/failed/skipped/expired/cancelled. AP and client targets require observed active anchor/association. CPU protection and measured CLI time budget take precedence; base scheduled polls run before detailed commands. Client-table and station-stats only, fixed allowlist. Job state and detailed cache are not durable across restarts yet. Unsupported/denied detail commands disabled for the current worker. No actual field data collected by CI.

The Engine is internal and exercised with fixture sessions. Existing --collect-once remains the only explicitly enabled actual device trial. Live continuous service, automatic candidate selection, secure web job APIs, verified CLI and actual Windows service validation are outstanding.
