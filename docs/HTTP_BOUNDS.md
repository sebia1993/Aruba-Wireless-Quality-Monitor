# Diagnostic HTTPS envelope limits (v0.34)

The diagnostic TLS listener still uses the configured source CIDRs, exact Host,
existing role/session/Origin checks and bounded connection pool. The HTTP
handler adds an independent 30-second socket lifetime starting after TLS
handshake; receiving one byte at a time cannot reset this timer. A successful,
admitted WSS upgrade disarms the HTTP timer under a lock and uses the existing
WSS ping, receive rate, session and maximum lifetime checks.

Request line <=8 KiB; aggregate headers <=32 KiB and each header line <=8 KiB.
The standard-library parser handles the bounded lines. Malformed header defects,
ambiguous Cookie fields, GET request bodies and Transfer-Encoding are rejected.
POST retains the existing 8 KiB exact form-body limit, strict length and
Content-Type validation. No request data, credentials or token is logged.

An expired HTTP connection is shut down, not a cancellation of an already
running DB or authentication operation. The existing bounded CLI/SSH and
storage limits still apply. A client can receive an unconfirmed result if a
state write was accepted but the response was interrupted; use the existing
idempotent receipts/revision checks. This does not constitute full DoS or
penetration testing. Unit and real synthetic loopback TLS regression checks
cover trickling headers, excessive header size and WSS handoff. Actual intranet
load, browser, service shutdown and security qualification remain separate.
