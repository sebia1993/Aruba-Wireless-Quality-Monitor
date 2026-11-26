# D08 protected selected AP ARM history observation

Original v1 section 3.2: anomaly/channel change, channel/power change history.
The existing documented-layout parser is used by the protected service only,
without new credentials, generic queue admission or browser target input.
Default off. A local administrator must verify the actual device layout,
read-only permission, target scope and physical identity before enabling.

With the service stopped, use the same protected selected-AP manifest schema
as docs/RF_SUMMARY_OBSERVATION.md, but file config/arm-history-validation.json,
contract aos8-arm-history-observation-v1 and exactly these checks:

- documented_history_layout
- ap_name_target_scope
- wired_identity_and_active_anchor
- switch_ip_owner
- read_only_permission
- selected_arm_investigation

All checks require actual local verification, not a CI result. Up to five
selected APs per controller, unique physical MAC across controller selections,
exact Radio tag and approval lifetime. Absent file or explicit disabled
manifest disables this path. Only standard profile; no automatic transfer.

The worker uses the existing D07/D04 physical AP preflight and a fresh
coherent Radio/active-AP mapping. Dispatch triggers are existing RF
investigation thresholds or a channel change between distinct coherent
observations of the same local physical Radio set in the same connection.
A gap, ambiguity, missing Radio, reconnect or band/Radio-set change is not
a channel change. Short-lived change triggers are consumed on one attempt;
incomplete intermediate observations can suppress a change trigger rather
than guessing. This is not a complete channel transition detector.

D07/D04/D08 share the same one-attempt/WLC/60s limiter, base and existing
reserved detail precedence, CPU and total/detail time budgets, oldest
eligible path first. D08 AP minimum cooldown 180s (conservative bound for
the event-triggered specification), including failed attempts. Unsupported
or denied responses latch off; raw output is never retained or published.

Admin operations displays the received time, reported interface/PHY,
reason code, raw old/new channel/power, and reported relative age, with up
to 64 rows per AP. No absolute event times, dBm inference, deduplication,
verified Radio binding, complete history, AP Down or cause classification.
Header-only/empty output is not proof of no change. Expired, stale,
inconsistent or disconnected context withdraws the current view. No
persistent history or retention change. Cross-WLC corroboration and
validated time/Radio binding are prerequisites for later RF cause use.

Actual Windows 11, corporate WLC/AAA and 72h run remain owner-owned tests,
not passed by hosted CI.
