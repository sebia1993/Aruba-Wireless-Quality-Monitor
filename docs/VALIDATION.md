# Synthetic full-capacity / real elapsed-time validation

The approved capacity is 4 WLCs, 2,000 APs, 4,000 Radios and 20,000 clients. The capacity harness synthesizes documentation-shaped CLI responses and drives real ControllerWorker, Engine, merge, Store and read-model paths without SSH or live data. It uses a controlled poll clock to complete the first collection cycle; elapsed performance durations are real. Ten concurrent in-process read clients issue 50 requests. It does not measure browser rendering, authenticated HTTPS load, real WLC latency, or Windows Service lifecycle.

Run a full capacity cycle in a NEW output directory:
```powershell
AWQM.exe --validate-scale capacity --output C:\AWQM-Validation\capacity-01
```
The CI development build runs the equivalent source command and stores only its result JSON (not synthetic database partitions) in the ZIP artifact. Fixture shape and small real-engine execution are also unit-tested.

## User-owned 72-hour continuous-operation validation

Scope update (2026-11-25): the owner will perform this test separately. The
developer need not execute or wait for the 72-hour trial to finish development.
Keep the validation utility and acceptance criteria available; never record the
trial as passed without actual results. This does not transfer other developer
integration, security, performance or Windows service lifecycle tests.

For the actual elapsed-time soak, use a dedicated Windows 11 x64 client PC with at least 100 GB free and a NEW directory. The process must run uninterrupted for 72 real hours. Do not use a production data directory, share credentials, or connect to actual WLCs.
```powershell
AWQM.exe --validate-scale soak --output D:\AWQM-Validation\soak-01
```
This uses real monotonic and wall clocks, all 4 full-capacity fixture workers, 60-second target scheduling, SQLite persistence and maintenance. It records periodic coverage and read-model timing. A short or accelerated test cannot qualify. Clock or execution gaps, storage/maintenance errors, missing coverage, or >2s read time fail the gate. Only the final result "passed" after 72 hours counts toward this synthetic soak. Hosted GitHub Windows runners are Windows Server, not the approved Windows 11 client; they cannot qualify the 72-hour gate.

Remaining independent gates: actual Win11 SCM installation/restart/stop/reboot with dedicated account and offline environment; long-term retained DB query performance; authenticated HTTPS concurrency and browser layout; security/dependency audit; production release checklist. The actual corporate WLC CLI/permission/AAA/Airtime verification remains operator-owned. This harness is not a promise of field compatibility.

## Actual loopback TLS/WSS capacity (v0.29)

Run `AWQM.exe --validate-scale https --output C:\\AWQM-Validation\\https-01` in a new synthetic destination. It runs a full fixture ingestion, starts only a 127.0.0.1 TLS listener with a temporary generated certificate and synthetic local verifier, opens ten distinct authenticated WSS connections, and issues 50 concurrent HTTPS dashboard/search requests while measuring real notification fanout. It tests duplicate and eleventh WSS admission rejection, cross-origin rejection, logout revocation, and response coverage/identity masking. HTTPS P95 <=2s and all ten WSS notification deliveries <=2s are the gate. Only the result JSON is included in the CI artifact. No test private key, raw database, tokens or credentials are exported. These numbers exclude browser rendering, enterprise AAA and real WLC delays. Actual browser, full retained DB, Windows SCM, and 72-hour qualification remain separate.


## Retained-history benchmark (v0.30)

The approved sizing model is 40,320,000 raw observations (7 days), 34,560,000 5-minute rows (30 days), and 35,040,000 hourly rows (365 days), for 109,920,000 historical rows with 4,000 Radios and one configuration epoch per bucket. The isolated generator materializes the real SQLite schemas and indexes with invented values; it bypasses the live ingestion and aggregate computation paths deliberately. Current latest rows use the real Store writer. The read benchmark calls the production diagnostic history read model and serializes the response to JSON. Five AP/Radio selections per resolution, exact cardinality/time/value/provenance checks, raw 24h P95 <=3s, 30d 5m and 365d hourly P95 <=5s.

CI runs 32 Radios (879,360 historical rows) over the full date windows. It is explicitly a representative smaller-cardinality read test. Full-cardinality execution is separate:

```powershell
AWQM.exe --validate-scale retained-full --output D:\AWQM-Validation\retained-full-01
```
Use a dedicated fixed local test drive, a nonexistent output directory, and at least 150 GB free. Actual storage can be tens of GB or more; reserve checks may stop generation. The harness does not remove or overwrite any existing data, and leaves its own synthetic data for inspection. Remove it manually only after reviewing the result. The result contains row counts, data bytes, generation time and measured per-query timing. It is not a cold-cache claim, concurrent writer stress, full ingestion/aggregation or backup/retention validation. A representative pass does not satisfy the full-scale read gate. Windows service lifecycle, browser rendering, security and 72-hour soak remain separate.


## SSH dependency regression (v0.32)

Run AWQM.exe --validate-ssh. Generates temporary in-memory 2048-bit
fixture RSA keys, starts ephemeral 127.0.0.1 servers, and performs actual
SSH handshake/authentication, a single read-only fixture CLI, auth-only
verification, wrong-password rejection and changed/missing key rejection.
No production SSH or AAA, no external network and no actual site data.
Both the unit suite and frozen executable run this test in Windows CI.
No real-controller compatibility claim. 72-hour execution remains user-owned.

## Representative actual SQLite lifecycle trial (v0.38)

Run `AWQM.exe --validate-scale lifecycle --output C:\\AWQM-Validation\\lifecycle-01` in a NEW isolated destination. CI uses 32 invented Radios and 120 minute cycles (3,840 rows) through the actual Store writer, deliberately interrupts one cycle after staging, reopens the Store and verifies recovered receipts and identical replay. It rebuilds both closed-day aggregate resolutions, checks every Radio's exact time/value/coverage, creates and checks a full raw+aggregate backup, restores into a separate directory, tests approved/unapproved retirement and verifies both surviving aggregate series and unchanged source data. Reports real per-cycle P95, aggregation, backup and restore seconds. The synthetic files remain in the new destination; only result JSON is packaged by CI. This is a correctness trial, not a full day/4,000-Radio writer or 109.92-million-row retained benchmark, concurrent writer, production recovery, browser, real-device, SCM or 72-hour qualification.

## Actual browser UI trial (v0.39)

Separate CI job installs `requirements-browser.txt` and the Playwright Chromium binary; neither is included in the packaged application. Run `python -m tools.browser_validation --output browser-validation` after installing the browser in an isolated test environment. Uses an invented 102-Radio Store, actual diagnostic TLS endpoint and synthetic identity verifier. Exercises 1440x900 light and 390x844 dark viewports, actual login and cookie session, pagination, search, Radio detail/history CSV download, detail clearing and fresh search on navigation, disabled search on unrelated menus, WebSocket publication, operator/admin boundaries, server-side revocation and logout. The fixture-only certificate exception does not change product TLS settings. Result JSON only; no private keys, tokens, downloads or DB artifacts. This is a representative Chromium interaction check on the CI runner, not physical Windows 11, production AAA, full browser load, cross-browser or real-device qualification.

## Native form regression (v0.40)

Initial v0.39 Chromium jobs failed before the dashboard at native login.
The no-referrer document policy conflicts with the strict Origin check for
navigation POSTs (WHATWG Fetch Origin serialization). Both HTTPS interfaces
now use same-origin for HTML only. The browser trial observes native login and
logout POST status/Origin (no header or cookie dump), and HTTP regression tests
reject null/foreign/missing Origin. Repeat browser CI is required; no production
authentication claim.

### Native CSV download isolation

v0.40 browser login succeeds with HTTP 303 and the real same-origin Origin. The native CSV download was canceled in Chromium. The next fixture revision uses a process-local exception for the generated certificate SPKI only, leaves approved HTTPS requests unintercepted, and independently verifies the exact certificate, response status, content headers, CSV content and masking before clicking the real download link. The saved file must match the verified endpoint bytes. No blanket TLS bypass, OS certificate-store change, mocked download or production application change. Repeat browser CI required.

The repeat [browser run for 084b41a](https://github.com/sebia1993/Aruba-Wireless-Quality-Monitor/actions/runs/45532402678) passed on both viewports, including a 381-byte native CSV that matched the HTTPS response.

The v0.41 browser fixture also opens the Radio history and incident detail in separate tabs, reloads the Radio detail, exports history and returns to the inventory. The URL fragment contains only the opaque Radio token or incident ID. Radio tokens are scoped to the service instance.

The owner deferred the 4,000-Radio retained-history trial until a fixed test volume with 150 GB free is available. Record its outcome separately from the representative retained-history and ingestion trials.

### Concurrent Chromium dashboard

Full candidate CI runs `python -m tools.browser_load_validation --output browser-load-validation` with the browser dependencies. Ten distinct synthetic operators open isolated Chromium sessions against the loopback HTTPS service. The trial ingests one full fixture cycle, then measures 50 searches and dashboard renders over 4,000 Radios. It checks the returned APs, the rendered results, notification delivery, administrator access denial and session revocation. The timing limits are two seconds for render P95 and for delivery of the publication to all ten sessions. The JSON records the browser version, runner platform and timings. Ingestion during the rendered reads, live AAA and the full retained database require separate evidence.
