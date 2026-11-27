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

Membership cross-check: self and directional peer observations must be at most 30 seconds apart. A fresh but nonaligned peer cannot establish agreement or disagreement with self. If self is connected and only nonaligned peers exist, display time_unaligned (cross-check withheld). Aligned and total peer counts are shown separately; without self the aligned count is unknown. Individual peer state/time remains in links, even when excluded from comparison. Self issues remain self observations; no Down, failover, common cause, or service impact is inferred. This is a temporal prerequisite for future CL02 and common incident analysis, not a completed cause rule.

Load context alignment: AP/client load and the reporting WLC's own complete membership observation must be within 30 seconds of each other, in addition to their existing independent freshness and mapping checks. A structurally valid but unaligned load is withheld (time_unaligned if no eligible source remains); a separate eligible source may still be reported. Cross-check/conflict comparison requires all four load/membership timestamps of the two sources to fit a common 30-second window. Publish the selected membership timestamp alongside the load timestamp. This is an observation-quality restriction only, not CL01/CL02, Down, failover, or service impact evidence. No polling cadence or command changes.
