# Synthetic full-capacity / real elapsed-time validation

The approved capacity is 4 WLCs, 2,000 APs, 4,000 Radios and 20,000 clients. The capacity harness synthesizes documentation-shaped CLI responses and drives real ControllerWorker, Engine, merge, Store and read-model paths without SSH or live data. It uses a controlled poll clock to complete the first collection cycle; elapsed performance durations are real. Ten concurrent in-process read clients issue 50 requests. It does not measure browser rendering, authenticated HTTPS load, real WLC latency, or Windows Service lifecycle.

Run a full capacity cycle in a NEW output directory:
```powershell
AWQM.exe --validate-scale capacity --output C:\AWQM-Validation\capacity-01
```
The CI development build runs the equivalent source command and stores only its result JSON (not synthetic database partitions) in the ZIP artifact. Fixture shape and small real-engine execution are also unit-tested.

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
