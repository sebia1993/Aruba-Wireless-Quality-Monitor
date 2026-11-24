# AWQM development progress (v0.15.0-dev)

Planning is approved and complete. This is a provisional engineering estimate,
not a measured test-pass percentage or field readiness claim. Each of the ten
approved work packages is assigned equal weight (10% of the development scope).
Each package estimate reflects implementation, integration, test evidence and
remaining work. The estimate excludes the user's separately owned real-device
field validation, but includes all developer-side release gates, including the
actual 72-hour soak and Windows service lifecycle checks.

| WP | Area | Estimated completion | Main outstanding requirement |
|---|---|---:|---|
| 01 | Foundation | 100% | Maintain contracts and fixtures |
| 02 | Collector and scheduling | 77% | Complete CLI catalog and validated adaptive detail workflow |
| 03 | CLI and cluster integration | 65% | Peer/load, UAC/BSSID/session/roaming coverage and provenance |
| 04 | Database lifecycle | 68% | Privacy/evidence retention, full backup and operational scale |
| 05 | Analysis | 72% | Full cause catalog, validated input gates and incident review |
| 06 | Dashboard | 75% | Full client/incident views, WebSocket and end-to-end operation |
| 07 | Security | 69% | Full audit lifecycle, hardening, production authorization tests |
| 08 | Windows operation | 65% | Actual SCM lifecycle, installer/update/rollback and offline verification |
| 09 | Validation | 25% | Full regression, load, security and 72-hour real-time soak |
| 10 | Distribution | 10% | Qualified release candidate and complete guides |

Arithmetic mean: 62.6% (rounded 63%). These estimates will be updated from verified evidence;
a successful development build is not a passed production release gate.
Windows builds through v0.14 verified. Latest v0.15 build requires confirmation. No production
release or real corporate equipment validation has been performed.
