# Build dependency advisory gate (v0.31)

The development Windows ZIP build records the installed Python distribution
versions in BUILD-DEPENDENCIES.txt. A separate Windows job downloads that
artifact from the same workflow, installs a pinned pip-audit tool in its
own environment and audits those exact frozen versions without resolving
a different dependency graph. The scanner uses its default PyPI advisory
source. Reference: https://github.com/pypa/pip-audit

The report checker requires all exact pins, one matching result per package,
no skipped packages, no reported known advisories, and a zero scanner exit.
Missing/invalid evidence, advisory-source errors, unpinned packages,
version mismatches or advisories block the security gate. The development
ZIP may still exist because its build is independent from this qualification.
No vulnerability exception or automatic upgrade/fix is used. Check the
dependency-audit job and AWQM-dependency-audit artifact before claiming a
pass. The scanner itself is not packaged in the runtime.

This verifies known Python package advisories at build time only. It does
not qualify native DLLs, Python/SQLite/OS patch status, license compliance,
code signing, threat modeling, actual Windows Defender or site security.
Those gates remain separate. No corporate data, credentials or CLI output
are submitted to advisory services.
