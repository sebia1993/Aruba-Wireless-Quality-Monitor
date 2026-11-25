"""Ten isolated Chromium sessions rendering a full synthetic AWQM inventory."""
import argparse
import asyncio
import json
import platform
import tempfile
import threading
import time
from contextlib import contextmanager
from ipaddress import IPv4Network
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import async_playwright

from awqm import __version__
from awqm.audit import AuditLog
from awqm.auth import Auth
from awqm.diagnostic_view import DiagnosticView
from awqm.diagnostic_web import RestrictedTLS, handler_for
from awqm.https_validation import certificate
from awqm.maintenance import write_json
from awqm.scale_validation import p95, scale_test, win11
from awqm.secure_preview import tls_context
from awqm.storage import Store
from awqm.web_config import WebConfig, cert_expiry
from tools.browser_validation import PASSWORD, fixture_spki, require


USERS = 10
ROUNDS = 5

# Compare the visible table to the independent scale_fixtures naming scheme.
# A matching prior render cannot satisfy the next action: each round switches
# between the full dashboard and a different exact AP query.
MATCHES = """query => {
  if (document.hidden) return false;
  const cardValues = [...document.querySelectorAll('#content .kpi strong')].map(c => c.textContent);
  if (cardValues.join('|') !== '4 / 4|4000|4000|20000') return false;
  const rows = [...document.querySelectorAll('#content > .table-wrap tbody tr')];
  if (rows.length !== (query ? 2 : 100)) return false;
  const pager = document.querySelector('#content > .pager span');
  if (pager?.textContent !== (query ? '1 / 1 · 2건' : '1 / 40 · 4000건')) return false;
  return rows.every((row, i) => {
    const name = query || ('DEMO-AP-' + String(1 + Math.floor(i/2)).padStart(4,'0'));
    const band = i % 2 ? '2.4GHz' : '5GHz';
    return row.cells[0]?.innerText === name && row.cells[2]?.textContent === band;
  });
}"""


class Signals:
    def __init__(self):
        self.ready = asyncio.Event()
        self.received = asyncio.Event()
        self.closed = asyncio.Event()
        self.wanted = None
        self.received_at = None

    def observe(self, socket):
        def frame(raw):
            if not isinstance(raw, str) or len(raw) > 256:
                return
            message = json.loads(raw)
            if message.get("type") != "publication" or message.get("available") is not True:
                return
            self.ready.set()
            if message.get("revision") == self.wanted and not self.received.is_set():
                self.received_at = time.perf_counter()
                self.received.set()
        socket.on("framereceived", frame)
        socket.on("close", lambda _: self.closed.set())


@contextmanager
def fixture_server(root):
    fixture = root / "capacity"
    capacity = scale_test(fixture)
    require(capacity["result"] == "pass", "full synthetic ingestion failed")
    store = Store(fixture / "data", mode="diagnostic")
    data = store.snapshot()
    current = [(r["radio_id"], r["observed_at"]) for r in data["radios"]]
    require(len(current) == 4000 and data["pending_writes"] == 0, "unexpected stored inventory")
    view = DiagnosticView(store, time.time)
    meta = {"coherent_controllers": 4, "expected_controllers": 4, "mapped_radios": len(current),
            "active_clients": capacity["client_count"]}
    def publish():
        view.publish(meta, current)
    publish()
    roles = {f"fixture-load-{n}": "operator" for n in range(USERS)}
    audit = AuditLog(root / "audit.db")
    auth = Auth(roles, lambda user, secret: secret == PASSWORD, audit_sink=audit.auth_event)
    cert, key = certificate(root)
    cfg = SimpleNamespace(**vars(WebConfig("127.0.0.1", "127.0.0.1:0", 0,
        (IPv4Network("127.0.0.1/32"),), "fixture", cert, key, roles, cert_expiry(cert, "127.0.0.1"))))
    stop = threading.Event()
    server = RestrictedTLS(cfg, handler_for(view, auth, audit, cfg, stop=stop), tls_context(cert, key))
    cfg.authority = f"127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "https://" + cfg.authority, cert, auth, view, publish
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join()


async def login(context, url, number, signal):
    page = await context.new_page()
    def websocket(ws):
        if ws.url == "wss://" + urlsplit(url).netloc + "/ws/updates":
            signal.observe(ws)
    page.on("websocket", websocket)
    page.set_default_timeout(15000)
    await page.goto(url)
    await page.locator('input[name=username]').fill(f"fixture-load-{number}")
    await page.locator('input[name=password]').fill(PASSWORD)
    async with page.expect_response(lambda response: response.url == url + "/auth/login") as pending:
        await page.locator('form[action="/auth/login"] button').click()
    response = await pending.value
    require(response.status == 303 and await response.request.header_value("origin") == url,
            "native login rejected")
    await page.wait_for_function(MATCHES, arg="")
    await asyncio.wait_for(signal.ready.wait(), 8)
    require(await page.locator("#role").inner_text() == "권한: operator", "operator identity missing")
    return page


async def render_read(page, url, query):
    def target(response):
        location = urlsplit(response.url)
        return (location.netloc == urlsplit(url).netloc and location.path == "/api/radios"
                and parse_qs(location.query, keep_blank_values=True) == {"q": [query], "page": ["1"]})
    start = time.perf_counter()
    async with page.expect_response(target) as pending:
        await page.locator("#search").fill(query)
    require((await pending.value).status == 200, "dashboard search failed")
    await page.wait_for_function(MATCHES, arg=query)
    # Include a rendering opportunity after the correct DOM was observed.
    await page.evaluate("() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))")
    return start, time.perf_counter()


async def exercise(root, report):
    with fixture_server(root) as (url, cert, auth, view, publish):
        async with async_playwright() as runner:
            browser = await runner.chromium.launch(args=["--ignore-certificate-errors-spki-list=" + fixture_spki(cert)])
            report["chromium_version"] = browser.version
            try:
                contexts, pages = [], []
                external_requests = []
                def outside_fixture(target):
                    location = urlsplit(target)
                    return location.scheme in ("https", "http") and (
                        location.scheme != "https" or location.netloc != urlsplit(url).netloc)
                async def deny_external(route):
                    external_requests.append(True)
                    await route.abort()
                signals = [Signals() for _ in range(USERS)]
                last_publish = time.monotonic()
                report["phase"] = "native_login_and_wss"
                for index, signal in enumerate(signals):
                    if time.monotonic() - last_publish >= 6:
                        await asyncio.to_thread(publish)
                        last_publish = time.monotonic()
                    context = await browser.new_context(viewport={"width": 1440, "height": 900})
                    await context.route(outside_fixture, deny_external)
                    contexts.append(context)
                    pages.append(await login(context, url, index, signal))
                report["authenticated_sessions"] = len(pages)
                report["phase"] = "concurrent_browser_render"
                samples, searches, dashboards = [], [], []
                for cycle in range(ROUNDS):
                    queries = [f"DEMO-AP-{(n * 199 + cycle + 1):04d}" if cycle % 2 == 0 else ""
                               for n in range(USERS)]
                    if cycle == 0:
                        revision = view.notification()["revision"] + 1
                        for signal in signals:
                            signal.wanted = revision
                        start = time.perf_counter()
                        result = await asyncio.gather(asyncio.to_thread(publish),
                            *(render_read(page, url, query) for page, query in zip(pages, queries)))
                        intervals = result[1:]
                        await asyncio.wait_for(asyncio.gather(*(s.received.wait() for s in signals)), 8)
                        wss = [signal.received_at - start for signal in signals]
                        report.update(wss_notifications=len(wss), wss_max_seconds=round(max(wss), 6))
                    else:
                        intervals = await asyncio.gather(*(render_read(page, url, query)
                                                           for page, query in zip(pages, queries)))
                    require(max(start for start, end in intervals) < min(end for start, end in intervals),
                            "ten timed operations did not overlap")
                    durations = [end - start for start, end in intervals]
                    samples.extend(durations)
                    (searches if queries[0] else dashboards).extend(durations)
                    report["rendered_reads"] = len(samples)
                report.update(render_p95_seconds=p95(samples), render_max_seconds=round(max(samples), 6),
                              exact_ap_searches=len(searches), search_p95_seconds=p95(searches),
                              full_dashboard_reads=len(dashboards), dashboard_p95_seconds=p95(dashboards),
                              overlapping_ten_reader_rounds=ROUNDS)
                report["phase"] = "operator_role_boundary"
                status_of = "async path => (await fetch(path, {cache:'no-store'})).status"
                denials = await asyncio.gather(*(page.evaluate(status_of, "/api/admin/status") for page in pages))
                require(all(status == 403 for status in denials), "operator role boundary failed")
                require(not any(signal.closed.is_set() for signal in signals), "publication connection lost")
                report.update(operator_denials=len(denials), phase="session_revocation")
                cookies = await contexts[0].cookies(url)
                token = next(c["value"] for c in cookies if c["name"] == "__Host-awqm_session")
                auth.logout(token, auth.session(token, touch=False).csrf)
                await asyncio.wait_for(signals[0].closed.wait(), 8)
                statuses = await asyncio.gather(*(page.evaluate(status_of, "/api/radios?q=&page=1") for page in pages))
                require(statuses == [401] + [200] * (USERS - 1), "session isolation failed")
                require(not any(signal.closed.is_set() for signal in signals[1:]), "unrelated publication socket closed")
                require(not external_requests, "fixture attempted external network access")
                report.update(external_http_requests=0, revoked_sessions=1, remaining_sessions=9,
                              phase="timing_gate")
                require(report["render_p95_seconds"] <= 2 and report["wss_max_seconds"] <= 2,
                        "browser render or publication latency exceeded two seconds")
            finally:
                await browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    root = parser.parse_args().output.absolute()
    root.mkdir(parents=True, exist_ok=False)
    report = {"kind": "synthetic_chromium_concurrent_dashboard", "version": __version__,
              "platform": platform.system(), "platform_release": platform.release(),
              "platform_version": platform.version(), "architecture": platform.machine(),
              "windows_11_x64": win11(), "ap_count": 2000, "radio_count": 4000, "client_count": 20000,
              "controllers": 4, "browser_processes": 1, "rendered_reads": 0, "render_p95_limit_seconds": 2,
              "live_devices": False, "enterprise_aaa": False, "concurrent_ingestion": False,
              "full_retained_database": False, "soak": False, "phase": "capacity_fixture", "result": "incomplete"}
    path = root / "browser-load-result.json"
    write_json(path, report)
    try:
        with tempfile.TemporaryDirectory(prefix="awqm-browser-load-") as tmp:
            asyncio.run(exercise(Path(tmp), report))
    except Exception as exc:
        report.update(result="fail", error_type=type(exc).__name__)
    else:
        report.update(result="pass", phase="complete")
    write_json(path, report)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["result"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
