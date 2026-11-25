# AWQM v1.0.0 requirements completion tracker

## Authority and scope
- User-approved final design, 2026-11-24, HTML document `AWQM_Final_Development_Specification_v1.0-draft.html` (Drive file ID `1ZvYbaTZ95eQ6gHrzfL5Qebjs79Wf_aB1`; original Markdown SHA-256 as recorded in HTML footer: `a8e49476e3d706c3a6abcc496ab7f468217c73db3c3313a16272fa5797527f69`).
- Baseline `16da39440ffe2195b205f22547ef79d5dc3004b0`, [full CI run 45604879302](https://github.com/sebia1993/Aruba-Wireless-Quality-Monitor/actions/runs/45604879302) passed six hosted jobs. That is NOT 100% of FR-01–12.
- Keep the later approved unified EXE, web-first setup, tray, current-user DPAPI and protected service boundaries. Never mark actual Windows 11, corporate WLC/AAA or actual 72-hour run passed without user-provided evidence. Owner executes those separately; preserve gates and validation utilities. Do not require them to start or finish developer-side implementation. No live WLC access or sensitive data in GitHub.
- Work on `feature/unified-launcher` only unless the owner authorizes a different branch. CI must correspond to the exact commit. Do not relax tests, release qualification, RBAC, host-key verification, backup/data protections or CPU/CLI limits. No automatic publication or permission workaround.

## Traceability (baseline audit, conservative)
Statuses are evidence-based; partial means primitives or conditional integration exist, not full acceptance. A slice is not complete until its new tests pass on the exact commit. The list is an initial backlog, not an exhaustive per-field audit.

| FR | WP / test | Existing evidence | Remaining work |
|---|---|---|---|
| FR-01 | WP02/03, T01–03 | worker, cluster, AP anchors, controller resources | reconcile local setup with protected service; conditionally supported cluster CLI and field attestation; avoid duplicate sessions |
| FR-02 | WP03, T01–03 | aos8, ap_anchors, association_history, BSS mapping | complete source/epoch/anchor/identity and conflict handling, full client/session lifecycle |
| FR-03 | WP02, T02/04 | scheduler, protection, durable/manual jobs | shared collection owner, full supported command catalog, adaptive targeted jobs and coverage evidence |
| FR-04 | WP05, T04 | client quality, live_clients, client_info_airtime, correlation | measured/estimated Airtime only from validated inputs; indirect candidate support; RF01/RF02; no fabricated percentage |
| FR-05 | WP03/05, T04 | RF neighbors, resource observations | missing detailed parsers/worker integration for Radio stats, uplink, ARM history and other approved conditional evidence |
| FR-06 | WP05, T04/06 | incidents, RF03–06, reviews | remaining RF01/02/07/08, AP01/02, CL01/02 and contradictory evidence/compound causes; UNKNOWN retained |
| FR-07 | WP05/06, T03–06 | quality ledger and incident timeline | cross-AP common incident grouping, recurrence, context and recovery with provenance |
| FR-08 | WP06, T02/06 | diagnostic global search/manual reservations | local cached cross-WLC search (first slice), complete unified search, interest targets and reservations with role and scope |
| FR-09 | WP06, T05/06 | diagnostic history, association history, reports | unified historical client/AP/incident views and exports; permission/masking and retention |
| FR-10 | WP04, T05 | store, retention, maintenance, rollback | complete configuration and evidence backup/restore, capacity and lifecycle evidence, safe web management |
| FR-11 | WP07, T07 | Aruba verifier, HTTPS/WSS, DPAPI, RBAC, audit | unified UI without privilege escalation, full session/privacy/retention cases; real AAA owner validation |
| FR-12 | WP08, T08 | single EXE, standard-user upgrade, hosted SCM | exact-candidate native Windows evidence owner-owned; preserve update/rollback and offline package integrity |

## Implementation order and boundaries
1. Establish traceability and strengthen local observation search across WLC slots (FR-08). Keep each source claim, show stale/paused state, never use a missing row as Down, no extra SSH, no raw client data. This is a small independently testable slice, not a completed global-search requirement.
2. Resolve the two collection paths by designing a single authoritative owner for shared service collection and a local setup/admin handoff. Compare `device_monitor.py` / `device_web.py` with `worker.py` / `service_runtime.py` / `diagnostic_web.py`. Do not run concurrent redundant WLC polling or expose protected service credentials to the desktop browser. Add regression tests before integration changes.
3. Build the remaining approved CLI command capability matrix, bounded parsers and fixture-driven workers; actual device support remains unknown until owner attestation. Prioritize safe RF/AP/controller evidence. No arbitrary CLI, no undocumented interpretation.
4. Complete quality and cause rules, temporal/epoch alignment, contradictory evidence, common incidents; preserve measured/estimated/indirect/UNKNOWN separation. Add boundary, missing data, conflicting evidence and false-positive tests.
5. Finish identity/history/retention/backup, integrated search and five-menu dashboard, management and authorization. Prefer existing tested primitives over duplicate implementations.
6. Map all T01–T08 and G1–G5 to evidence; run required candidate CI, record full-scale limitations and user-owned trials. Produce exact-candidate offline artifact, hashes, SBOM and guides. Do not claim v1.0.0 production readiness or publish a formal release unless release conditions/permissions are satisfied.

## Current increment
- FR-08 slice: local `search` action uses only cached AP/Radio/SSID/BSSID display identifiers and observed WLC slot/IP. Case-insensitive matching and exact alternate-format BSSID matching; 100-row pages, 128-character bound, separate source/freshness columns, no new collector activity, no merging or raw client identity. Existing loopback token/Origin checks apply.
- Tests: `tests/test_local_observation_search.py`, HTTP capability checks in `test_device_settings_entry.py`, actual Chromium checks in `tools/browser_device_validation.py`. Full CI requested; status pending until the new commit is verified in Actions.
- Next slice after CI: inspect single-owner service/local handoff and add a bounded interface/capability contract and integration tests; if a feature depends on unavailable field semantics, record the dependency and proceed on safe independent features.
