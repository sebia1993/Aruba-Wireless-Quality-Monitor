# Optional local Radio metric validation (v0.11.0-dev)

The default remains field-unvalidated diagnostic collection. The developer
has not checked the enterprise controllers. An authorized field operator
may attest Radio metric semantics locally after completing the checks below.
This is not validation of client Airtime, retry semantics, service impact,
root cause, production readiness or the 72-hour release gate.

1. Check the actual registered WLC's model/version using show version and
   independently verify the pinned SSH host key (OpenSSH SHA256 format).
   The documented model/version line is parsed strictly:
   https://arubanetworking.hpe.com/techdocs/ArubaOS/oxygen/AOS-8%20Command%20Line%20Interface%20Guide/s/show-version.html
2. Check NF in dBm, utilization/interference in percent, actual active
   anchor roles, wired MAC identity, physical Radio ID mapping, service
   Radio modes and observation timing. Do not attest any unknown field.
3. Stop the service. Under the approved ACL-protected ProgramData/AWQM/config,
   create radio-validation.json. By default the file can be absent or
   contain {"version":1,"enabled":false}. The enabled schema requires
   site, contract="aos8-radio-v1", and controllers (list).
   Each controller record requires controller (registered ID), ip, model
   (exact show version string), firmware (exact AOS 8 version), host_key
   ("SHA256:..." verified actual key), approved_at and valid_until
   (integer Unix epoch seconds; maximum validity 365 days), reviewed_by
   (local operator reference) and checks (object with all of these keys
   explicitly true only after verification):
   nf_dbm, utilization_percent, interference_percent, physical_radio_id,
   wired_mac_identity, active_anchor_role, service_radio_mode,
   observation_timing. Never use example or guessed approvals.
4. Restart. show version is scheduled only for attested controllers.
   A mismatched firmware/host key, stale version result, changed SSH
   generation, expired approval, invalid Radio mapping or incomplete
   observations withhold the current score. Raw source remains
   aos8-cli-unvalidated. Approval is never generated automatically.

Separate core DB tables radio_quality_latest and radio_quality_events
store locally attested Radio policy evaluation and context changes.
The balanced policy uses existing 50/30/20 score, three valid observations
for persistence and three for recovery with hysteresis. Safe profile
uses a 270-second maximum interval gap (180-second polling target).
Different contexts begin a new evidence sequence without closing old
events. Missing data is UNKNOWN, not recovery. Cached results are not
reprocessed at each coordinator pulse. The authenticated diagnostic web
view exposes current locally attested scores only; stale/expired results
are hidden. Raw history and exports remain field-unvalidated observations.
Automatic real client-detail investigation, complete incident lifecycle,
validated client quality/Airtime and production release are still pending.
