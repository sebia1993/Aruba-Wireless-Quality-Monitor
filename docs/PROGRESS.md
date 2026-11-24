# AWQM development progress (v0.29.0-dev)

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
| 02 | Collector and scheduling | 94% | Complete CLI catalog and validated adaptive detail workflow |
| 03 | CLI and cluster integration | 81% | Peer/load, UAC/BSSID/session/roaming coverage and provenance |
| 04 | Database lifecycle | 85% | Privacy/evidence retention, full backup and operational scale |
| 05 | Analysis | 84% | Full cause catalog, validated input gates and incident review |
| 06 | Dashboard | 97% | Full client/incident views, WebSocket and end-to-end operation |
| 07 | Security | 80% | Full audit lifecycle, hardening, production authorization tests |
| 08 | Windows operation | 80% | Actual SCM lifecycle, installer/update/rollback and offline verification |
| 09 | Validation | 42% | Full regression, load, security and 72-hour real-time soak |
| 10 | Distribution | 18% | Qualified release candidate and complete guides |

Arithmetic mean: 76.1%. These estimates will be updated from verified evidence;
a successful development build is not a passed production release gate.
v0.20 corrected Windows CI passed (135 tests, one platform skip); full synthetic capacity cycle and offline ZIP verification succeeded. v0.21 Windows CI and full capacity cycle passed. v0.22 Windows CI, full capacity cycle and history regression passed. v0.23 Windows CI and package build passed. v0.24 initial CI failed on SQLite fixture handle cleanup; corrected explicit closure; 157 tests (156 passed, one platform skip), full synthetic capacity and Windows ZIP build passed. v0.25 unit/integration and Windows package verification passed. v0.26 authenticated WSS and Windows package verification passed. v0.27 correlation regression and full synthetic capacity passed; package verification tracked in Actions. v0.28 aggregate RF03 evidence unit/integration and full synthetic capacity passed; Windows package tracked in Actions. v0.29 full synthetic authenticated loopback HTTPS/WSS capacity test added; results pending. No production
release or real corporate equipment validation has been performed.
