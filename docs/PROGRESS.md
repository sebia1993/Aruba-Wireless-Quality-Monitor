# AWQM development progress (v0.40.1-dev portable integration)

Planning is approved and complete. This is a provisional engineering estimate,
not a measured test-pass percentage or field readiness claim. Each of the ten
approved work packages is assigned equal weight (10% of the development scope).
Each package estimate reflects implementation, integration, test evidence and
remaining work. The estimate excludes the user's separately owned real-device
field validation and, per the owner's scope update on 2026-11-25, execution of
and assessment of the actual 72-hour continuous-operation trial. The trial is
user-owned and pending, not waived as a production-readiness claim. Windows
service lifecycle checks, shorter automated tests, capacity, security, and
remaining developer-side release gates stay in the development scope.

| WP | Area | Estimated completion | Main outstanding requirement |
|---|---|---:|---|
| 01 | Foundation | 100% | Maintain contracts and fixtures |
| 02 | Collector and scheduling | 95% | Complete CLI catalog and validated adaptive detail workflow |
| 03 | CLI and cluster integration | 88% | Peer/load, UAC/BSSID/session/roaming coverage and provenance |
| 04 | Database lifecycle | 88% | Privacy/evidence retention, full backup and operational scale |
| 05 | Analysis | 85% | Full cause catalog, validated input gates and incident review |
| 06 | Dashboard | 97% | Full client/incident views, WebSocket and end-to-end operation |
| 07 | Security | 85% | Full audit lifecycle, hardening, production authorization tests |
| 08 | Windows operation | 83% | Actual SCM lifecycle, installer/update/rollback and offline verification |
| 09 | Validation | 50% | Full regression, load, security and Windows/browser validation (72-hour trial: user-owned) |
| 10 | Distribution | 18% | Qualified release candidate and complete guides |

Arithmetic mean: 78.9% (provisional; no percentage awarded for transferring responsibility). These estimates will be updated from verified evidence;
a successful development build is not a passed production release gate.
v0.20 corrected Windows CI passed (135 tests, one platform skip); full synthetic capacity cycle and offline ZIP verification succeeded. v0.21 Windows CI and full capacity cycle passed. v0.22 Windows CI, full capacity cycle and history regression passed. v0.23 Windows CI and package build passed. v0.24 initial CI failed on SQLite fixture handle cleanup; corrected explicit closure; 157 tests (156 passed, one platform skip), full synthetic capacity and Windows ZIP build passed. v0.25 unit/integration and Windows package verification passed. v0.26 authenticated WSS and Windows package verification passed. v0.27 correlation regression and full synthetic capacity passed; package verification tracked in Actions. v0.28 aggregate RF03 evidence unit/integration and full synthetic capacity passed; Windows package tracked in Actions. v0.29 Windows CI, authenticated loopback HTTPS/WSS and ZIP verified. v0.30 representative retained-history benchmark (879,360 rows), Windows CI and ZIP passed; full-cardinality run pending. v0.31 Windows build and regression passed; dependency-audit job blocked on
reported advisories in Paramiko 4.0.0 and cryptography 49.0.0 (run 45520334108).
v0.32 pins Paramiko 5.1.0 and cryptography 50.0.2 and adds real loopback SSH/host-key regression plus frozen validation. v0.32 Windows CI and dependency audit passed (45525207273). v0.33 Windows CI and ZIP passed (191 tests, 190 passed, one platform skip), including AP anchor observations, synthetic capacity, HTTPS/WSS and representative retained history. v0.34 Windows CI and dependency advisory audit passed (45527318272). v0.35 adds stopped/disabled post-start data/log rollback, preserved newer trees and journaled resumption. Initial CI: two fault-injection fixture failures (Windows normalized path comparison); corrected in v0.36, repeat verification pending. v0.36 adds offline client-info TID layout/pair inspection and numeric contract hardening; no live Airtime. CI tracked in Actions. The user
will separately perform the 72-hour trial; keep the existing soak tool and
record its status as not executed by the developer, not passed. No production
release or real corporate equipment validation has been performed.


v0.36 Windows CI and exact dependency audit passed (45528221718). v0.37 fixes duplicate/retry aggregate invalidation and queue accounting, with four storage regression cases. CI pending; the provisional 78.9% estimate is unchanged until verified evidence warrants reassessment. Full retained cardinality and Windows service qualification remain open.


v0.38 adds representative actual Store lifecycle validation: 32 Radios / 120 minute cycles, durable stage interruption and restart, duplicate/revision safety, exact raw/aggregate verification, checksummed backup/restore and approved retirement on the restored synthetic copy. CI pending; 78.9% unchanged pending verified results and reassessment. Full-cardinality throughput and service qualification still open.


v0.39 fixes stale detail/search after menu navigation, isolates in-flight callbacks, and adds separate actual Chromium browser CI for operator/admin login, search/pagination, detail/history export, WSS and revocation at two viewports. CI pending; existing 78.9% estimate retained until verified evidence. Browser dependencies are excluded from build packaging.


v0.37.1 portable build and exact dependency audit passed (45530631175). v0.39.1 portable integration incorporates v0.39, including lifecycle and browser UI gates. Progress remains provisional 78.9%; no new production release or field qualification.


v0.40.1-dev: include the complete v0.40.0-dev native browser form fix and strict Origin regression, preserving single EXE restrictions. Both v0.39 browser jobs failed at login. Repeat CI pending; 78.9% unchanged.