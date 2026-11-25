# Authenticated real-time publication notifications (v0.26)

The diagnostic dashboard uses WSS on the existing HTTPS listener at
/ws/updates. The server sends only publication revision and availability.
It never receives application commands, returns telemetry over WSS, or
triggers a CLI read. All detailed data still comes from authenticated,
role-checked HTTPS APIs. The receiver uses wsproto's protocol parser with
no negotiated compression or subprotocol.

One socket per authenticated session, at most ten total notification
sockets within the twenty-four-connection TLS limit, leaving capacity for ordinary concurrent HTTPS requests. Exact Host and
Origin, cookie session, certificate lifetime and role validity are checked;
session expiry and logout close the stream. Notifications never refresh
idle login lifetime. Incoming application text/binary messages close the
connection. Bounded control-frame rates, short I/O deadlines, ping/pong,
five-minute maximum socket lifetime and service shutdown prevent permanent
notification threads. Browser reconnect and HTTPS fallback preserve
operation if WSS is unavailable. A hidden tab releases its socket.
The existing page/search/detail context is not replaced by pushed data; overview/Radio automatic refresh restores table scroll offsets. Background session checks continue on history/admin without replacing active forms.

CI exercises real local TLS/WebSocket handshake, publication notifications,
session revocation and client message rejection. Actual intranet proxy,
browser layout/latency, ten-browser concurrency and 72-hour validation
remain separate qualification gates. No external CDN or public service.
