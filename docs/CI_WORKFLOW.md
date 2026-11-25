# CI/CD lanes — development and qualification

Ordinary development pushes use quick, change-aware Windows source checks:
runtime dependencies, compile check, explicit leaf test groups, baseline
and demo smoke. Unknown changes, requirements, broad/shared modules or
missing/untrusted diff ranges fall back to all unit tests. Portable sync
merges use the verified foundation second parent as diff baseline; portable
launcher tests always run. Quick checks are not release qualification.

The independent browser workflow runs on relevant foundation web changes
with its own concurrency group. It does not build EXEs or audit build
dependencies. Use it to diagnose login, CSV, rendering and WSS regressions.
Portable syncs do not repeat this browser job automatically; the portable
candidate must still pass the complete browser gate itself.

Full Windows qualification is opt-in per branch: include `[awqm-full]`
in the head commit message, or use workflow_dispatch when available.
GitHub manual dispatch requires the workflow to exist on the default
branch; before that, the explicit push marker is the reliable entry point.
The full gate preserves all unit tests, capacity, HTTPS/WSS, history,
SQLite lifecycle, frozen executable and package verification, exact build
dependency audit, and actual Chromium tests. Full runs use a SHA-specific
concurrency group and are not cancelled by a later development commit.
Quick/browser jobs supersede obsolete quick/browser runs only.

On the portable branch, `[publish-awqm-portable]` still explicitly invokes
the complete candidate workflow and can publish a development prerelease
only after Windows, exact dependency audit and Chromium jobs all succeed
on that exact commit. It is not an approved production release. Do not
use the publication marker without authorization. No full gate is replaced
by a fast check or by a passing result from another commit.

Recommended cycle: batch core changes on foundation, run quick/browser
checks as appropriate, integrate once into portable, then explicitly run
full candidate qualification. Record distinct outcomes for source checks,
build, browser, dependency audit and remaining field qualification.
