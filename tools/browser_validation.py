"""Actual headless Chromium UI test against isolated loopback diagnostic HTTPS.

Run only in the separate browser-test environment. Browser dependencies are
not shipped in AWQM. All identities, measurements and certificates invented.
"""
import argparse
import json
import tempfile
import threading
import time
from ipaddress import IPv4Network
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect
from awqm import __version__
from awqm.audit import AuditLog
from awqm.auth import Auth
from awqm.diagnostic_view import DiagnosticView
from awqm.diagnostic_web import RestrictedTLS, handler_for
from awqm.https_validation import certificate
from awqm.maintenance import write_json
from awqm.model import Radio
from awqm.secure_preview import tls_context
from awqm.storage import Store
from awqm.web_config import WebConfig, cert_expiry

PASSWORD="synthetic-browser-only"
ROLES={"fixture-operator":"operator","fixture-admin":"admin"}


def require(condition,message):
    if not condition:
        raise RuntimeError(message)


def fixture(root):
    now=int(time.time())
    store=Store(root/"data",mode="diagnostic")
    rows=[]
    for n in range(1,103):
        ident=f"browser-fixture-{n:04d}"
        rows.append(Radio(ident,ident,f"DEMO-BROWSER-{n:04d}","WLC-1","5GHz",44,
                          now,40,5,-94,2,source="aos8-cli-unvalidated",epoch="fixture-1"))
    store.save_batch(rows)
    view=DiagnosticView(store,lambda:now)
    meta={"coherent_controllers":1,"expected_controllers":4,"mapped_radios":len(rows),
          "active_clients":204}
    view.publish(meta,[(r.radio_id,r.observed_at) for r in rows])
    return view


def submit_form(page,url,action,button):
    # Observe, never inject, the native browser's Origin. Log only status
    # and the origin classification, not request headers/bodies/cookies.
    with page.expect_response(lambda r:r.url==url+action and r.request.method=="POST") as pending:
        button.click()
    response=pending.value
    origin=response.request.header_value("origin")
    classification="same-origin" if origin==url else "null" if origin=="null" else "missing" if origin is None else "unexpected"
    print(f"Native form {action}: status={response.status}, origin={classification}",flush=True)
    require(classification=="same-origin" and response.status==303,
            f"native form rejected: {action}, status={response.status}, origin={classification}")


def login(page,url,role):
    response=page.goto(url,wait_until="domcontentloaded")
    require(response is not None and response.header_value("referrer-policy")=="same-origin",
            "login document referrer policy mismatch")
    expect(page.locator('input[name="username"]')).to_be_visible()
    page.locator('input[name="username"]').fill("fixture-"+role)
    page.locator('input[name="password"]').fill(PASSWORD)
    submit_form(page,url,"/auth/login",page.get_by_role("button",name="로그인",exact=True))
    expect(page.locator("#role")).to_have_text("권한: "+role)
    expect(page.locator("#nav")).to_be_visible()


def browser_test(output):
    output=Path(output).absolute()
    output.mkdir(parents=True,exist_ok=False)
    result_path=output/"browser-result.json"
    result={"kind":"synthetic_actual_chromium_ui","version":__version__,"result":"incomplete",
            "real_device_validated":False,"real_aaa_validated":False,
            "windows_11_browser_validated":False,"full_capacity_browser_load_validated":False,
            "scm_validated":False,"soak_validated":False,"scenarios":{}}
    write_json(result_path,result)
    with tempfile.TemporaryDirectory(prefix="awqm-browser-") as tmp:
        root=Path(tmp)
        view=fixture(root)
        audit=AuditLog(root/"audit.db")
        auth=Auth(ROLES,lambda u,p:p==PASSWORD,audit_sink=audit.auth_event)
        cert,key=certificate(root)
        cfg=SimpleNamespace(**vars(WebConfig("127.0.0.1","127.0.0.1:0",0,
            (IPv4Network("127.0.0.1/32"),),"fixture",cert,key,ROLES,
            cert_expiry(cert,"127.0.0.1"))))
        stop=threading.Event()
        server=RestrictedTLS(cfg,handler_for(view,auth,audit,cfg,stop=stop),tls_context(cert,key))
        cfg.authority=f"127.0.0.1:{server.server_port}"
        url="https://"+cfg.authority
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True)
                try:
                    result["browser_version"]=browser.version
                    # Ignore certificate trust only for this isolated generated
                    # loopback fixture; transport/host verification is tested by
                    # the independent TLS validation. Block external HTTP(S).
                    for width,height,theme in ((1440,900,"light"),(390,844,"dark")):
                        context=browser.new_context(viewport={"width":width,"height":height},
                            color_scheme=theme,ignore_https_errors=True,accept_downloads=True)
                        try:
                            external=[]
                            def route_request(route):
                                target=urlsplit(route.request.url)
                                if target.scheme not in ("https","wss") or target.netloc!=cfg.authority:
                                    external.append(True);route.abort()
                                else:route.continue_()
                            context.route("**/*",route_request)
                            page=context.new_page()
                            page.set_default_timeout(12000)
                            errors=[];frames=[]
                            page.on("pageerror",lambda error:errors.append(True))
                            def observe_ws(ws):
                                if ws.url=="wss://"+cfg.authority+"/ws/updates":
                                    ws.on("framereceived",lambda frame:frames.append(frame))
                            page.on("websocket",observe_ws)
                            login(page,url,"operator")
                            expect(page.locator("#content .kpis")).to_be_visible()
                            page.locator('[data-view="radios"]').click()
                            table=page.locator("#content > .table-wrap tbody tr")
                            expect(table).to_have_count(100)
                            page.get_by_role("button",name="다음",exact=True).click()
                            expect(table).to_have_count(2)
                            page.locator("#search").fill("DEMO-BROWSER-0001")
                            expect(table).to_have_count(1)
                            page.get_by_role("button",name="DEMO-BROWSER-0001",exact=True).click()
                            expect(page.locator("#detail")).to_be_visible()
                            page.get_by_role("button",name="이력 조회",exact=True).click()
                            expect(page.get_by_role("link",name="CSV 다운로드")).to_be_visible()
                            with page.expect_download() as pending:
                                page.get_by_role("link",name="CSV 다운로드").click()
                            download=pending.value
                            require(download.suggested_filename.endswith(".csv"),"wrong export filename")
                            require(b"browser-fixture-" not in Path(download.path()).read_bytes(),
                                    "raw Radio identity leaked to export")
                            page.locator('[data-view="clients"]').click()
                            expect(page.locator("#detail")).to_be_hidden()
                            expect(page.locator("#detailBody")).to_be_empty()
                            expect(page.get_by_text("단말 상세 수집 비활성",exact=False)).to_be_visible()
                            page.locator("#search").fill("DEMO-BROWSER-0102")
                            page.locator('[data-view="radios"]').click()
                            expect(table).to_have_count(1)
                            expect(page.get_by_role("button",name="DEMO-BROWSER-0102",exact=True)).to_be_visible()
                            page.locator('[data-view="history"]').click()
                            expect(page.locator("#search")).to_be_disabled()
                            expect(page.get_by_text("저장된 정책 위험 이력 없음",exact=False)).to_be_visible()
                            page.locator('[data-view="admin"]').click()
                            expect(page.locator("#search")).to_be_disabled()
                            require(context.request.get(url+"/api/admin/status").status==403,
                                    "operator admin endpoint not denied")
                            page.locator('[data-view="radios"]').click()
                            expect(page.locator("#search")).to_be_enabled()
                            expect(table).to_have_count(1)
                            # The table has its own intentional horizontal scroll;
                            # the page itself must fit the selected viewport.
                            require(page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"),
                                    "page overflows viewport")
                            until=time.monotonic()+5
                            while not frames and time.monotonic()<until:
                                page.wait_for_timeout(100)
                            require(bool(frames),"no actual browser WebSocket frame")
                            require(any(json.loads(frame).get("type")=="publication" for frame in frames
                                        if isinstance(frame,str) and frame.startswith("{")),
                                    "missing publication frame")
                            if theme=="light":
                                # Server-side session revocation while UI is open.
                                with auth.lock:
                                    auth.sessions.clear()
                                page.locator("#search").fill("DEMO-BROWSER-0002")
                                expect(page.get_by_text("인증 만료. 새로고침 후 다시 로그인하세요.",exact=True)).to_be_visible()
                                expect(page.locator("#detail")).to_be_hidden()
                            else:
                                submit_form(page,url,"/auth/logout",page.get_by_role("button",name="로그아웃",exact=True))
                                expect(page.locator('input[name="username"]')).to_be_visible()
                                require(context.request.get(url+"/api/radios?q=&page=1").status==401,
                                        "logout did not revoke API access")
                            require(not errors and not external,"browser script error or external request")
                            result["scenarios"][f"{width}x{height}-{theme}"]="pass"
                        finally:
                            context.close()
                    context=browser.new_context(ignore_https_errors=True)
                    try:
                        page=context.new_page()
                        login(page,url,"admin")
                        page.locator('[data-view="admin"]').click()
                        page.get_by_role("button",name="관리자 상태 조회",exact=True).click()
                        expect(page.get_by_text("감사 기록",exact=False)).to_be_visible()
                        require(context.request.get(url+"/api/admin/status").status==200,
                                "admin endpoint unavailable")
                        submit_form(page,url,"/auth/logout",page.get_by_role("button",name="로그아웃",exact=True))
                        expect(page.locator('input[name="username"]')).to_be_visible()
                        result["scenarios"]["admin_login_status_logout"]="pass"
                    finally:
                        context.close()
                finally:
                    browser.close()
            result["result"]="pass"
            write_json(result_path,result)
            return result
        except BaseException:
            write_json(result_path,result)
            raise
        finally:
            stop.set();server.shutdown();server.server_close();thread.join(5)


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(browser_test(args.output),ensure_ascii=False))
