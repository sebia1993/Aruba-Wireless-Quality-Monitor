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

## Windows native runtime review, 2026-11-25

The earlier sections describe the Python distribution audit. This review covers
the **v0.42.0-dev folder ZIP**, commit `ed1451261083dbd7e73fb672e23ef36719ddc44d`,
SHA-256 `d762330eb5952a47a7c3906b3f426615f520a5f2f2a8b7cccf0321cea64815b0`.
The DLL version resources identify CPython 3.12.10, SQLite 3.49.1 and OpenSSL
3.0.16. The bundled cryptography 50.0.2 `_rust.pyd` contains the OpenSSL 4.0.3
version string, consistent with its Windows wheel release notes.
[Folder build](https://github.com/sebia1993/Aruba-Wireless-Quality-Monitor/actions/runs/45541222743) · [cryptography 50.0.2](https://cryptography.io/en/50.0.2/changelog/)

| Shipped component | Finding and application use |
| --- | --- |
| CPython 3.12.10 | PSF rates CVE-2025-4517 (TAR extraction) and CVE-2026-19445 (TLS server SNI callback switching contexts) Critical. AWQM stages ZIPs and uses a single server TLS context. A separate Low finding, CVE-2026-15310, affects some ZIP compression methods. Staging checks the trusted archive digest before decompression. The exact v0.42 ZIP uses stored and deflated members, both outside the affected methods. [Python 3.12.11](https://www.python.org/downloads/release/python-31211/) · [Python 3.12.15](https://www.python.org/downloads/release/python-31215/) |
| OpenSSL 3.0.16 in CPython | Three vendor High findings apply to this version, CVE-2025-15467 (CMS), CVE-2026-45447 (PKCS#7) and CVE-2026-84782 (DTLS). The diagnostic listener uses TCP TLS with a locally configured PEM certificate. The reviewed application entry points do not accept CMS, PKCS#7 or DTLS input. [OpenSSL 3.0 advisories](https://openssl-library.org/news/vulnerabilities-3.0/) |
| SQLite 3.49.1 | CVE-2025-6965 is High and fixed in 3.50.2. It requires control over SQL. AWQM supplies its queries and binds user inputs. Other upstream fixes through 3.54.0 concern database contents, including FTS5 indexes and WAL files. Snapshot verification opens protected local database files, so damaged or substituted input remains relevant to recovery. [SQLite’s CVE dispositions](https://www.sqlite.org/cves.html) · [NVD severity](https://nvd.nist.gov/vuln/detail/CVE-2025-6965) |

### Replacement candidate

The Windows x64 build pins **CPython 3.14.10 with SQLite 3.54.1**.
The official CPython archive contains OpenSSL 3.5.9 and SQLite 3.50.4.
SQLite publishes 3.54.1 separately. Its x64 DLL exports all 87 SQLite
symbols imported by the CPython 3.14.10 `_sqlite3.pyd`.
[Python 3.14.10](https://www.python.org/downloads/release/python-31410/) · [SQLite 3.54.1 release](https://www.sqlite.org/releaselog/3_54_1.html)

The AWQM store uses WAL. Both SQLite 3.49.1 and 3.50.4 fall in the affected range for the WAL-reset race between connections writing or checkpointing the same database. The selected SQLite version includes the upstream correction. [SQLite WAL notes](https://www.sqlite.org/wal.html#the_wal_reset_bug)

The official 1,450,919-byte SQLite ZIP matched the vendor SHA3-256
`c2d09c8babb9399277055c057c97a53f1ffd7c5f11d0c1f2118ffdfa39f2daa4`.
The extracted `sqlite3.dll` SHA-256 was
`4b59d3cded9dc1608544160f3b4b10ac974c6ac0d6ffc23064e5f4c8dbd05aea`.
[SQLite download manifest](https://www.sqlite.org/download.html) · [Versioned x64 archive](https://www.sqlite.org/2026/sqlite-dll-win-x64-3540100.zip)

OpenSSL's November 24 advisories for 3.5.9 and the wheel's 4.0.3 backend
rate the QUIC, DANE client and X.509 CRL distribution-point issues Low.
AWQM exposes TCP HTTPS and pinned SSH. Certificate and CRL processing
depends on the deployed trust configuration. Review both backends when
refreshing the candidate. [OpenSSL 3.5 advisories](https://openssl-library.org/news/vulnerabilities-3.5/) · [OpenSSL 4.0 advisories](https://openssl-library.org/news/vulnerabilities-4.0/)

Qualification of a rebuilt ZIP requires its own digest, frozen runtime
version checks for both OpenSSL backends and SQLite, and the full candidate
test results. Production release also requires the target Windows patch
level, site authentication and deployment controls, and release approval.
