# AWQM development progress (v0.21.0-dev)

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
| 02 | Collector and scheduling | 90% | Complete CLI catalog and validated adaptive detail workflow |
| 03 | CLI and cluster integration | 81% | Peer/load, UAC/BSSID/session/roaming coverage and provenance |
| 04 | Database lifecycle | 80% | Privacy/evidence retention, full backup and operational scale |
| 05 | Analysis | 78% | Full cause catalog, validated input gates and incident review |
| 06 | Dashboard | 89% | Full client/incident views, WebSocket and end-to-end operation |
| 07 | Security | 76% | Full audit lifecycle, hardening, production authorization tests |
| 08 | Windows operation | 69% | Actual SCM lifecycle, installer/update/rollback and offline verification |
| 09 | Validation | 37% | Full regression, load, security and 72-hour real-time soak |
| 10 | Distribution | 13% | Qualified release candidate and complete guides |

Arithmetic mean: 71.3%. These estimates will be updated from verified evidence;
a successful development build is not a passed production release gate.
v0.20 corrected Windows CI passed (135 tests, one platform skip); full synthetic capacity cycle and offline ZIP verification succeeded. Latest v0.21 regression/build pending. No production
release or real corporate equipment validation has been performed.
