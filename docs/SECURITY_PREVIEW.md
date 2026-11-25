# v0.8.0-dev: local HTTPS authentication preview and DPAPI

This is a local, synthetic dashboard with optional existing Aruba management
credential verification. It is NOT a production intranet service. No real
telemetry or client identity is loaded. No new web account or web password
database is created. The current preview uses one fixed approved WLC IP
and a verified known_hosts file, no SSH CLI shell and no authentication
fallback. Actual SSH login support and corporate authorization are field
verification requirements. Do not use unauthorized credentials or bypass
certificate warnings. The preview has no production collection session, so
shared authentication/collection coordination remains pending.

Local protected configuration (example only; documentation IP):

```json
{
  "auth_host": "192.0.2.10",
  "known_hosts": "C:/Approved/known_hosts",
  "cert": "C:/Approved/awqm-local-cert.pem",
  "key": "C:/Approved/awqm-local-key.pem",
  "roles": {"example-operator": "operator", "example-admin": "admin"}
}
```

Use a trusted certificate with the correct localhost/127.0.0.1 SAN and an
ACL-protected private key and configuration. Never commit those files.
`AWQM.exe --auth-preview --authorized --config C:/Approved/auth.json`
binds ONLY to 127.0.0.1:8443. TLS 1.2+, Secure/HttpOnly/SameSite cookies,
same-origin POST, logout CSRF check, role mapping, 30m idle/8h maximum,
bounded sessions/attempts and one temporary SSH verification at a time.
Background snapshot polling does not extend idle expiry. Server-side role
changes revoke sessions in memory. Central AAA account revocation is not
instantaneous; reauthentication is required. Audit is memory only.
Actual personal passwords are not persisted or logged.

`AWQM.exe --dpapi-smoke-test` performs a round trip for random synthetic
bytes under the current Windows identity. The primitive uses user scope
and CRYPTPROTECT_UI_FORBIDDEN, not machine scope. It does not yet store
collector credentials or install a service. The service identity/profile
and NTFS ACL, migration/re-entry and real Windows 11 testing remain
release requirements. Windows hosted CI is not the field service account.
