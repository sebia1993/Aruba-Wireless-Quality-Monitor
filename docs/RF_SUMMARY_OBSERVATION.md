# D04 protected surrounding-channel observation

D04 is the non-verbose, documented Channel Summary subset. It is default
off and independent from D07. Only the protected service in standard mode
can schedule it. It uses the existing current-generation physical AP
binding and Switch IP check, exact locally approved Radio version/key,
fresh AP inventory/AAC and Radio summary, and a unique physical Radio match
with utilization >=75%, interference >=30%, or noise >-80 in the locally
attested Radio summary. These are investigation triggers, not conclusions.
Missing or ambiguous readings cannot trigger a query.

After owner field validation, an authorized local administrator may prepare
the ACL-protected config/rf-summary-validation.json with the service stopped.
The schema is the same as the D07 example in AP_PORT_OBSERVATION.md, except
contract is "aos8-rf-summary-observation-v1" and the required checks are:

- documented_channel_layout
- ap_name_target_scope
- wired_identity_and_active_anchor
- switch_ip_owner
- read_only_permission
- selected_rf_investigation

All checks must be true, based on actual owner validation rather than this
document or CI. Each command requires its own approval; the exact Radio
tag, lifetime and selected AP name/wired MAC rules remain the same. An
absent file or explicit disabled manifest leaves D04 inactive. No browser
parameter or new generic detail queue can enable the command.

Existing base polls and queued detail investigations take precedence.
Selected D04 and D07 share one attempt/WLC/60 seconds, and D04 is limited
to one attempt per AP/180 seconds (D07 remains 120 seconds), subject to the
existing shared CPU and CLI time budgets. A denial or unsupported command
latches only that command off. No extra SSH sessions or auto-failover.
Oldest eligible selected command path is served first.

The admin-only operations screen displays up to 64 channel rows per AP,
the full parsed count, truncation and unparsed secondary section count.
All raw counters and index components/totals preserve exact decimal
strings, and inconsistent totals are identified without repair. The
observation is discarded on changed receiving AP/Radio trigger context,
expiry, failure or reconnect. CLI receipt time is not scan time. Reported
noise, utilization and channel values are not measured Airtime or
verified physical-radio measurements. No cause/Down inference, raw CLI,
MAC/IP disclosure, persistent history or D12 verbose support is added.
Real Windows 11, actual WLC/AAA and actual 72h remain owner validation.
