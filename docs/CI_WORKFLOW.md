# CI/CD lanes — development and qualification

For the unified Windows distribution introduced in v0.45, the full Windows workflow builds and validates one public `AWQM-Setup.exe`. It also produces the internal service archive for the protected update and rollback procedure. The [installation guide](../README.md) covers operator setup. The foundation and portable development branches retain the following validation lanes.

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

The corrected selector at 084b41a passed the [quick checks](https://github.com/sebia1993/Aruba-Wireless-Quality-Monitor/actions/runs/45532402766).
Both Chromium viewports saved a native CSV identical to the independently
verified HTTPS response in the [browser run](https://github.com/sebia1993/Aruba-Wireless-Quality-Monitor/actions/runs/45532402678).

## Windows service lifecycle

The full folder-package workflow installs the frozen service under a temporary, dedicated local account on a disposable GitHub-hosted Windows Server runner. The fixture configures a synthetic controller and suspends its collection before startup. The service must decrypt its own DPAPI credential and publish a running heartbeat with collection paused. The job exercises the SCM failure-recovery policy, upgrades from a separately built v0.42.0 baseline, and restores the previous installation after a candidate start. The artifact records each verified phase and the candidate ZIP digest. Reboot on the target Windows 11 edition, field collection and the long run remain separate qualification steps.

### Windows 11 first-launch release check

Run the following from a verification environment with Python 3.12+ and pywin32 on a clean, non-elevated Windows 11 profile. The check opens the installer without arguments on an isolated desktop, observes the dashboard window and browser request, reads the dashboard and sample data, relaunches from Start, closes the app and removes its test installation. Retain the JSON result with the tested installer's digest. GitHub's Windows Server job separately checks current-user installation, the Start menu entry and removal.

```powershell
python tools/windows11_installer_validation.py .\AWQM-Setup.exe --sha256 $expectedHash --output .\first-launch-result
```
