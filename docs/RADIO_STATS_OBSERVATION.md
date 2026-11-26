# D03 selected Radio statistics observation

Approved specification section 3.2: show ap debug radio-stats
ap-name <AP_NAME> radio <RADIO_ID> advanced, anomalous Radio, 120s.
The standard-mode protected service has an optional implementation.
Default off; existing desktop and generic reservation API do not admit
this command. The command builder accepts only a structured (AP name,
integer Radio 0/1/2) target, not a free-form CLI suffix.

After local operator validation of command/output and identity semantics,
create config/radio-stats-validation.json in the existing ACL-protected
service configuration (service stopped). Use the selected-AP manifest
schema in AP_PORT_OBSERVATION.md with contract
aos8-radio-stats-observation-v1 and exactly these true checks:

- documented_radio_statistics_subset
- advanced_command_scope
- physical_radio_id_argument
- wired_identity_and_active_anchor
- switch_ip_owner
- read_only_permission
- selected_radio_investigation

Exact local Radio approval tag and lifetime required. At most five
selected APs per WLC, unique physical AP bindings across WLCs.
Only a fresh, uniquely mapped anomalous service Radio is queried
(U >=75, I >=30 or NF >-80; all three fields must be present).
Unknown/ambiguous mapping, changed identity or host key, expired
approval and stale evidence block dispatch. No automatic failover.
Each Radio has a minimum 120s attempt interval. D03/D04/D07/D08 share
one selected attempt per WLC per 60s and existing CPU/time budgets;
base polls and reservations retain priority. This is not a guarantee
of the 120s target at capacity.

The admin collection status shows request AP/Radio and receipt time,
bounded recognized counters (decimal strings, null preserved) and
separate reported busy/Tx/Rx windows. No raw response, physical MAC/IP,
new session, retained history, delta, Airtime, cause or availability
inference. Only the documented subset is supported, not the entire
advanced AP-535 layout. Target echo, sample time, counter epoch and
completeness remain unverified. Invalid context or failed attempts
withdraw affected observations; denial/unsupported latches until restart.
Real device validation remains owner-owned; never attest based on CI alone.
