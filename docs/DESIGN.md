# AWQM approved design baseline — sanitized public summary

Approved 2026-11-24. No actual customer identifiers, addresses, credentials or device output.

1. Windows 11 x64, four WLCs in one cluster, all SSIDs, 2,000 APs / 4,000 radios / 20,000 clients target. Shared intranet HTTPS, read-only collection.
2. SSH CLI only, persistent worker per WLC, one command per controller. Adaptive polling, 60 s radio target, partial failures tolerated; no MM or REST device collection.
3. Stable AP ID from wired MAC and site; Radio ID is physical, not band; active/standby dedup; AAC/UAC independent. Failover, roaming, stale data, counter epochs and provenance must be preserved. No false Down from missing output.
4. Quality detection: balanced policy 75% utilization, 30% interference, noise > -80 dBm; three fresh observations to persistent, hysteresis recovery (65%, 20%, -85 dBm) three fresh samples. Scores 50/30/20 weights for radio and 40/35/25 for client. Actual Airtime only after field/units/scope verification, otherwise estimation then indirect evidence. No fabricated zero or cause certainty. Operator validation required; actual service impact separate from RF metrics.
5. SQLite WAL, daily raw, monthly aggregates, single writer, idempotency, 7d raw / 30d 5-minute / 365d hourly; client and roaming 30d; incident evidence 365d with privacy controls. Importance-based backups, disk protection.
6. Five menus: overview, AP, clients, incidents, administration. Global search, detail panel and full page, system color scheme, no external CDN, report HTML/CSV and copy summary.
7. Existing Aruba personal identity for web login, not a new account/password system. Separate collection credential, DPAPI under service identity, approved role mapping, HTTPS, host key checks, read-only CLI allowlist. Windows 11 x64 offline setup through one public AWQM-Setup.exe. The installed AWQM.exe provides the desktop dashboard, service manager and Windows service host. The system installation preserves automatic recovery and includes the protected internal service-update archive. No production remote access before approved authentication/security implementation.
8. All integration/security/Windows tests, strict release gates, P0/P1 zero, unresolved high/critical vulnerability zero. Actual enterprise controller testing and the 72h real-time soak are user-owned (owner scope update 2026-11-25). Retain the 72h acceptance criteria and tool; exclude execution from developer completion, and record qualification only upon receipt of actual results.
9. Build and development on a separate branch; no production release until release gates pass.

Core CLI candidates: show version; show cpuload; show lc-cluster group-membership; show lc-cluster load distribution ap/client; show ap radio-summary; show ap active details; show ap database long; show ap association; show user-table; show ap bss-table; show ap standby; show user-table standby; show memory; show datapath utilization. Detailed diagnostics are selected only for abnormal AP/client/radio. Actual command availability and response format on target firmware remain unverified. No unsupported command fallback to arbitrary CLI.

See STATUS.md for implementation versus design gaps.
