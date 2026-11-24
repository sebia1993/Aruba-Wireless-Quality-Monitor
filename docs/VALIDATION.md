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
