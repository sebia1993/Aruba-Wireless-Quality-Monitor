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


## v0.32 remediation

The v0.31 exact audit reported PYSEC-2026-2858 for Paramiko 4.0.0 and
PYSEC-2026-3552 for cryptography 49.0.0 (duplicate advisory rows included).
Pin Paramiko 5.1.0 and cryptography 50.0.2, newer than the scanner's listed
fixed versions 5.0.0 and 50.0.0. References:
- https://www.paramiko.org/changelog.html
- https://cryptography.io/en/stable/changelog/

The new frozen and source loopback SSH regression checks real RSA/SHA-2
handshake, pinned host key, fixed allowlisted CLI, authentication-only
path, wrong-password rejection and changed/missing key rejection.
Missing/changed key and incompatible negotiation must not trigger a
legacy algorithm fallback. Paramiko 5 removes RSA/SHA-1 signatures and
SHA-1 key exchanges; older WLC firmware that offers only legacy
algorithms may be incompatible. A known_hosts line containing RSA key
material is not itself a requirement to negotiate an SHA-1 signature.
Verify actual AOS 8.10.0.24 negotiation locally with site approval;
do not disable verification or downgrade to an affected version.
The new build and exact advisory scan must pass before remediation is
considered verified. No security waiver is introduced.
