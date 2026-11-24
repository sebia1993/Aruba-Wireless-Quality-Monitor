# v0.10.0-dev — optional diagnostic service HTTPS

This is not an approved production release. The Windows service keeps
read-only diagnostic collection and optional HTTPS read-only display in
one process. No live quality score, root-cause attribution, client MAC,
SSID or user detail is exposed. Browser polling reads published observations
and does not start CLI commands. No WebSocket or production device-control
API is enabled.

The service's protected ProgramData/AWQM/config/web.json is optional.
Absent or exactly {"version":1,"enabled":false} disables the listener.
The following is a documentation-only example (TEST-NET; replace locally
with authorized parameters). It assumes service.json contains WLC-1 and
uses a separate collection account.

```json
{
 "version":1, "enabled":true, "authorized":true,
 "bind":"192.0.2.50", "hostname":"awqm.example.invalid", "port":8443,
 "allowed_cidrs":["192.0.2.0/24"], "auth_controller":"WLC-1",
 "cert":"secrets/web-cert.pem", "key":"secrets/web-key.pem",
 "roles":{"example-operator":"operator","example-admin":"admin"}
}
```

Use the approved fixed interface and source CIDR, internal CA certificate
with matching exact SAN, protected TLS key, approved Windows firewall/GPO,
and existing Aruba personal IDs. Do not upload any real values or private
keys to GitHub. No wildcard bind, broad /0 allowlist, or automatic firewall
changes. Default recommendation remains loopback before approval. Use the
configured HTTPS hostname including port; do not bypass trust warnings.

Personal authentication uses only the registered authentication controller,
with pinned known_hosts, no CLI shell, and the same connection gate as
background collection. A busy worker, unknown/high CPU, unavailable session,
or shutdown defers login; no fallback or repeated authentication on another
controller. Existing collection IDs are excluded from the web role map.
Defaults: at most two login attempts per user per 300-second window, 20
global attempts; actual AAA policy remains a site requirement. Secure
host-only cookie, idle/absolute session limits, same-origin POST, logout
CSRF and role checks. Session role mapping changes require a service restart.
Central AAA revocation is not instant.

The service displays stored Radio observations, source validity warning,
current merge coverage and stale/unconfirmed status. Search uses asset
name, logical WLC, band and channel, not raw internal radio MAC identifiers.
Raw history limited to seven days and 2,000 rows; missing data stays unknown.
CSV/HTML export is field-unvalidated and contains no internal Radio MAC.
An existing authorized user may read old observations when collection fails.
Reports and login/logout/admin status access are recorded in protected
logs/web-audit.db, not including credentials or tokens. Audit retention and
comprehensive backup remain pending. Access to audit records is local
administrative only; the admin endpoint returns counts, not identities.

Actual network sharing, certificate chain/client trust, Windows firewall,
service ACL, WLC account/CPU and 72-hour field operation are not verified
by hosted CI. Source HTTPS integration tests use generated certificates
and invented data, not real WLCs. The original synthetic demo and local
authentication preview remain separate.
