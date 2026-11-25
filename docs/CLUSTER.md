# v0.16.0-dev cluster observations

AOS 8 CLI reference: https://arubanetworking.hpe.com/techdocs/CLI-Bank/Content/aos8/sh-lc-cluster.htm

Service polls group-membership on each registered WLC (60-second target).
One representative worker at a time runs load distribution ap/client
(120-second target), sharing existing CPU and CLI limits. An alternate
reader is selected periodically or when a fresh suitable alternative exists
after failure. Already-running representative work returns before switching.
Per-source read time and logical WLC ID are preserved. Controller IDs are
mapped using the registered management addresses; a deployment with distinct
cluster addresses needs an explicitly verified mapping (not guessed).
Membership has no count footer; absent peers stay unknown. Unknown addresses
are not exposed. Preamble configuration and VRRP fields are not stored.
AP/client load tables require unique self, complete registered member set,
consistent row totals and a recent connected membership observation.
Totals are from one source, not sum of multiple WLC tables. If two fresh
sources within 30s disagree, withhold that total. Time-aligned agreement
is labeled crosschecked; not a guarantee of actual field semantics.
CPU, self and peer observations are shown independently. SSH failure,
peer disagreement or incompatible membership cannot prove WLC Hang/Down,
AP loss or service impact. All observations remain field-unvalidated.
Offline commands: AWQM.exe --validate-cli cluster / ap-load / client-load
with the appropriate local output file. No actual WLC access performed.

Collection defaults to safe startup. Protected config/collection.json may contain {"version":1,"profile":"standard","authorized":true} after site approval to use the 60-second Radio target; {"version":1,"profile":"safe"} restores safe mode after restart. Adaptive CPU protection remains active. Independently fresh WLC snapshots are reconciled; duplicate physical wired-MAC owners remain unresolved.
