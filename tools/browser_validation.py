"""Actual headless Chromium UI test against isolated loopback diagnostic HTTPS.

Run only in the separate browser-test environment. Browser dependencies are
not shipped in AWQM. All identities, measurements and certificates invented.
"""
import argparse
import base64
import csv
import hashlib
import io
import json
import ssl
import tempfile
import threading
import time
from http.client import HTTPSConnection
from ipaddress import IPv4Network
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import urlsplit, urljoin
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from playwright.sync_api import sync_playwright, expect
from awqm import __version__
from awqm.audit import AuditLog
from awqm.aos8 import ControllerSnapshot
from awqm.auth import Auth
from awqm.diagnostic_view import DiagnosticView
from awqm.diagnostic_web import RestrictedTLS, handler_for
from awqm.https_validation import certificate
from awqm.maintenance import write_json
from awqm.model import Radio
from awqm.quality_ledger import QualityLedger
from awqm.secure_preview import tls_context
from awqm.storage import Store
from awqm.selected_history import SelectedHistory, Receipt, PORT, STATS, RF, ARM
from awqm.web_config import WebConfig, cert_expiry

PASSWORD="synthetic-browser-only"
ROLES={"fixture-operator":"operator","fixture-admin":"admin"}


def require(condition,message):
    if not condition:
        raise RuntimeError(message)


def fixture_spki(cert):
    # Chromium's download path may not honor a page-level TLS exception.
    # Scope the exception to this ephemeral fixture key, never all hosts.
    key=x509.load_pem_x509_certificate(cert.read_bytes()).public_key()
    der=key.public_bytes(Encoding.DER,PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(hashlib.sha256(der).digest()).decode("ascii")


def verified_get(context,url,path,cert):
    """Independent HTTPS response check using the exact fixture trust anchor."""
    u=urlsplit(urljoin(url,path))
    require(u.scheme=="https" and u.netloc==urlsplit(url).netloc,
            "fixture request escaped the approved authority")
    cookies=context.cookies(url)
    headers={"Cookie":"; ".join(c["name"]+"="+c["value"] for c in cookies)}
    trust=ssl.create_default_context(cafile=str(cert))
    connection=HTTPSConnection(u.hostname,u.port,context=trust,timeout=5)
    try:
        connection.request("GET",u.path+("?"+u.query if u.query else ""),headers=headers)
        response=connection.getresponse()
        data=response.read(2*1024*1024+1)
        require(len(data)<=2*1024*1024,"unexpected fixture response size")
        return response.status,dict(response.getheaders()),data
    finally:
        connection.close()


def validate_csv(data):
    require(b"browser-fixture-" not in data,"raw Radio identity leaked to export")
    rows=list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
    require(len(rows)==1 and rows[0]["mode"]=="diagnostic" and
            rows[0]["ap_name"]=="DEMO-BROWSER-0001" and rows[0]["source"]=="aos8-cli-unvalidated",
            "downloaded CSV row or provenance mismatch")


def fixture(root):
    now=int(time.time())
    store=Store(root/"data",mode="diagnostic")
    rows=[]
    for n in range(1,103):
        ident=f"browser-fixture-{n:04d}"
        # Two Radio observations of one physical AP (rename between observations).
        # Keep list size, individual names and existing history fixtures unchanged.
        ap="fixture:02:00:00:00:00:01" if n in (1,2) else ident
        rid=ap+":"+str(n-1) if n in (1,2) else ident
        rows.append(Radio(rid,ap,f"DEMO-BROWSER-{n:04d}","WLC-1","5GHz",44,
                          now,40,5,-94,2,source="aos8-cli-unvalidated",epoch="fixture-1"))
    store.save_batch(rows)
    ledger=QualityLedger(store)
    risk=replace(rows[-1],utilization=85,observed_at=now+1)
    store.save(risk)
    rows[-1]=risk
    snap=ControllerSnapshot(risk.controller,risk.observed_at,[],[],[],field_tag="fixture",
                            field_until=now+3600,field_connection="fixture",field_profile="safe")
    quality=ledger.evaluate([risk],[snap],now+1,{risk.radio_id:risk.observed_at})
    archive=SelectedHistory(store)
    for n in range(21):
        payload={"schema":1,"controller":"WLC-1","command":PORT,"received_at":now,
                 "source":"selected-cli-unvalidated","ap_binding":"a"*64,
                 "measurement_start":None,"measurement_end":None,"counter_epoch":None,
                 "connection":"PRIVATE-HISTORY-CONNECTION","observation":{
                     "ap_name":"PRIVATE-HISTORY-AP","received_at":now,"field_validated":False,
                     "sample_time_verified":False,"row_count_verified":False,
                     "physical_uplink_verified":False,"ports":[
                         {"port":0,"admin":"enabled","oper":"up","speed_mbps":2500,
                          "duplex":"full","tx_packets":"0","tx_bytes":"18446744073709551615",
                          "rx_packets":None,"rx_bytes":"300","stp":"PRIVATE-HISTORY-STP"}]
                     + ([{"port":1,"admin":"disabled","oper":"down","speed_mbps":None,
                          "duplex":None,"tx_packets":None,"tx_bytes":None,
                          "rx_packets":"0","rx_bytes":None}] if n==20 else [])}}
        archive.save(Receipt(f"{n+1:032x}","WLC-1",PORT,now,json.dumps(payload)))
    stats_payload={"schema":1,"controller":"WLC-1","command":STATS,"received_at":now,
                   "source":"selected-cli-unvalidated","ap_binding":"b"*64,
                   "measurement_start":None,"measurement_end":None,"counter_epoch":None,
                   "observation":{"ap_name":"PRIVATE-HISTORY-RADIO","received_at":now,
                       "field_validated":False,"sample_time_verified":False,"requested_radio":1,
                       "observed_field_count":5,"ignored_row_count":0,
                       "counters":{"radio_resets":"18446744073709551615","tx_timeouts":None,
                                   "rx_crc_errors":"0","private":"PRIVATE-HISTORY-VALUE"},
                       "radio_percentages":{"channel_busy_1s_percent":50,
                                            "channel_busy_4s_percent":42.5,
                                            "channel_busy_64s_percent":0}}}
    archive.save(Receipt(f"{22:032x}","WLC-1",STATS,now,json.dumps(stats_payload)))
    rf_payload={"schema":1,"controller":"WLC-1","command":RF,"received_at":now,
                "source":"selected-cli-unvalidated","ap_binding":"c"*64,
                "measurement_start":None,"measurement_end":None,"counter_epoch":None,
                "observation":{"ap_name":"PRIVATE-HISTORY-RF","received_at":now,
                    "field_validated":False,"sample_time_verified":False,
                    "layout":"band","row_count":2,"channels":[
                        {"channel":36,"reported_band":"5GHz","noise_raw":"92*",
                         "split_scan_reported":True,"retry_raw":"18446744073709551615",
                         "phy_error_raw":"0","mac_error_raw":None,
                         "utilization":{"channel_utilization":8,"rx":3,"tx":2,
                             "extension_channel_utilization":1,"quality":95,
                             "private":"PRIVATE-HISTORY-VALUE"}},
                        {"channel":44,"reported_band":"5GHz","noise_raw":None,
                         "split_scan_reported":None,"retry_raw":None,
                         "phy_error_raw":"0","mac_error_raw":None,"utilization":None}]}}
    archive.save(Receipt(f"{23:032x}","WLC-1",RF,now,json.dumps(rf_payload)))
    arm_time=now-7200
    arm_payload={"schema":1,"controller":"WLC-1","command":ARM,"received_at":arm_time,
                 "source":"selected-cli-unvalidated","ap_binding":"d"*64,
                 "measurement_start":None,"measurement_end":None,"counter_epoch":None,
                 "observation":{"ap_name":"PRIVATE-HISTORY-ARM","received_at":arm_time,
                     "field_validated":False,"sample_time_verified":False,"target_verified":False,
                     "radio_binding_verified":False,"completeness_verified":False,"event_time_verified":False,
                     "interface_count":2,"event_count":1,"truncated":False,"interfaces":[
                         {"interface":"wifi0","phy_type":"5GHz","table_present":True,"event_count":1,
                          "events":[{"reason_code":"OFF(R)","old_channel":"44-","new_channel":"0",
                                     "old_power":None,"new_power":"0","reported_age_seconds":62,
                                     "private":"PRIVATE-HISTORY-ARM-EVENT"}]},
                         {"interface":"wifi1","phy_type":"2.4GHz","table_present":False,
                          "event_count":0,"events":[]}]}}
    archive.save(Receipt(f"{24:032x}","WLC-1",ARM,arm_time,json.dumps(arm_payload)))
    view=DiagnosticView(store,lambda:now+1,incidents=ledger.incidents,
                        selected_history=archive,selected_controllers=("WLC-1",))
    meta={"coherent_controllers":1,"expected_controllers":4,"mapped_radios":len(rows),
          "active_clients":204}
    client = dict(id="c"*32, ap_name=risk.ap_name, controller=risk.controller, band=risk.band,
                  valid_until=now+300, observed_at=now, health=72, snr=19, last_phy_rate=144,
                  quality_score=63, quality_status="degraded", private_marker="internal-client-fixture")
    view.publish(meta,[(r.radio_id,r.observed_at) for r in rows],quality=quality,
                 client_views=[client],client_enabled=True)
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


def login(page,url,role,fragment=""):
    response=page.goto(url+fragment,wait_until="domcontentloaded")
    require(response is not None and response.header_value("referrer-policy")=="same-origin",
            "login document referrer policy mismatch")
    expect(page.locator('input[name="username"]')).to_be_visible()
    page.locator('input[name="username"]').fill("fixture-"+role)
    page.locator('input[name="password"]').fill(PASSWORD)
    submit_form(page,url,"/auth/login",page.get_by_role("button",name="로그인",exact=True))
    expect(page.locator("#role")).to_have_text("권한: "+role)
    if not fragment:expect(page.locator("#nav")).to_be_visible()


def cross_menu_search(page):
    page.locator('[data-view="admin"]').click()
    search = page.get_by_role("search", name="통합 검색")
    search.get_by_role("searchbox").fill("DEMO-BROWSER-0102")
    search.get_by_role("button", name="검색", exact=True).click()
    for heading in ("AP / Radio", "사용자 단말", "장애 이력"):
        expect(page.get_by_role("region", name=heading).get_by_role("button", name="상세 보기")).to_be_visible()
    page.get_by_role("region", name="AP / Radio").get_by_role("button", name="상세 보기").click()
    expect(page.locator("#detailBody").get_by_role("button", name="이력 조회", exact=True)).to_be_visible()
    page.get_by_role("region", name="장애 이력").get_by_role("button", name="상세 보기").click()
    expect(page.locator("#detailBody").get_by_role("link", name="CSV 보고서")).to_be_visible()
    clients = page.get_by_role("region", name="사용자 단말")
    clients.get_by_role("button", name="상세 보기").click()
    expect(page.locator("#detailBody").get_by_text("72%")).to_be_visible()
    with page.expect_popup() as popup:
        page.locator("#detailBody").get_by_role("link", name="전체 페이지에서 열기").click()
    detail = popup.value
    try:
        expect(detail.get_by_role("heading", name="단말 관측 상세", exact=True).first).to_be_visible()
        detail.reload()
        expect(detail.locator("#detailBody").get_by_text("72%")).to_be_visible()
        expect(detail.locator("body")).not_to_contain_text("internal-client-fixture")
    finally:
        detail.close()
    search.get_by_role("searchbox").fill("DEMO-BROWSER")
    search.get_by_role("searchbox").press("Enter")
    radios = page.get_by_role("region", name="AP / Radio")
    expect(radios.get_by_text("1 / 13 · 102건")).to_be_visible()
    radios.get_by_role("button", name="다음").click()
    expect(radios.get_by_text("2 / 13 · 102건")).to_be_visible()
    expect(radios.locator("tbody tr td:first-child")).to_have_text(
        [f"DEMO-BROWSER-{n:04d}" for n in range(9,17)])
    search.get_by_role("searchbox").fill("MISSING-FIXTURE-ONLY")
    search.get_by_role("searchbox").press("Enter")
    for heading in ("AP / Radio", "사용자 단말", "장애 이력"):
        expect(page.get_by_role("region", name=heading).get_by_text("검색 결과 없음")).to_be_visible()
    expect(page.locator("#detail")).to_be_hidden()


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
                browser=p.chromium.launch(headless=True,args=[
                    "--ignore-certificate-errors-spki-list="+fixture_spki(cert)])
                try:
                    result["browser_version"]=browser.version
                    # Only the ephemeral fixture certificate's SPKI is accepted
                    # by the test Chromium process. Independent HTTPS checks
                    # verify the exact certificate and host; no product TLS
                    # setting or operating-system trust store is changed.
                    for width,height,theme in ((1440,900,"light"),(390,844,"dark")):
                        context=browser.new_context(viewport={"width":width,"height":height},
                            color_scheme=theme,accept_downloads=True)
                        try:
                            external=[]
                            def outside_fixture(target_url):
                                target=urlsplit(target_url)
                                return target.scheme in ("http","https") and (
                                    target.scheme!="https" or target.netloc!=cfg.authority)
                            def reject_external(route):
                                external.append(True);route.abort()
                            # Do not intercept approved native downloads; route
                            # interception itself can alter download behavior.
                            context.route(outside_fixture,reject_external)
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
                            page.get_by_role("button",name="동일 물리 AP Radio 조회",exact=True).click()
                            related=page.locator(".physical-ap-panel tbody tr")
                            expect(related).to_have_count(2)
                            expect(related).to_contain_text(["DEMO-BROWSER-0001","DEMO-BROWSER-0002"])
                            with page.expect_popup() as popup:
                                page.get_by_role("link", name="전체 페이지에서 열기").click()
                            detail_page = popup.value
                            try:
                                expect(detail_page.locator("#detailBody h3").first).to_contain_text("DEMO-BROWSER-0001")
                                expect(detail_page.locator("#content")).to_be_hidden()
                                detail_page.reload()
                                detail_page.get_by_role("button", name="이력 조회", exact=True).click()
                                expect(detail_page.get_by_role("link", name="CSV 다운로드")).to_be_visible()
                                detail_page.get_by_role("link", name="목록으로 돌아가기").click()
                                expect(detail_page.locator("#content .kpis")).to_be_visible()
                            finally:
                                detail_page.close()
                            page.get_by_role("button",name="이력 조회",exact=True).click()
                            expect(page.get_by_role("link",name="CSV 다운로드")).to_be_visible()
                            link=page.get_by_role("link",name="CSV 다운로드")
                            href=link.get_attribute("href")
                            require(href is not None,"missing CSV export link")
                            status,headers,expected_csv=verified_get(context,url,href,cert)
                            print(f"Verified CSV endpoint: status={status}, bytes={len(expected_csv)}",flush=True)
                            require(status==200 and headers.get("Content-Type","").startswith("text/csv")
                                    and "attachment" in headers.get("Content-Disposition","")
                                    and int(headers.get("Content-Length","-1"))==len(expected_csv),
                                    "invalid CSV response headers")
                            validate_csv(expected_csv)
                            with page.expect_download() as pending:
                                link.click()
                            download=pending.value
                            require(download.suggested_filename=="awqm-diagnostic-history.csv",
                                    "wrong export filename")
                            failure=download.failure()
                            print("Native CSV download: "+("complete" if failure is None else "failed"),flush=True)
                            require(failure is None,"native CSV download failed after verified server response")
                            actual_csv=Path(download.path()).read_bytes()
                            validate_csv(actual_csv)
                            require(actual_csv==expected_csv,"native CSV bytes differ from HTTPS response")
                            result["scenarios"][f"{width}x{height}-{theme}-csv"]="pass"
                            page.locator('[data-view="clients"]').click()
                            expect(page.locator("#detail")).to_be_hidden()
                            expect(page.locator("#detailBody")).to_be_empty()
                            expect(page.get_by_text("현장 승인된 AP 정밀 조회의 임시 단말 관측",exact=False)).to_be_visible()
                            page.locator("#search").fill("DEMO-BROWSER-0102")
                            page.locator('[data-view="radios"]').click()
                            expect(table).to_have_count(1)
                            expect(page.get_by_role("button",name="DEMO-BROWSER-0102",exact=True)).to_be_visible()
                            page.locator('[data-view="history"]').click()
                            expect(page.locator("#search")).to_be_disabled()
                            page.locator("#content").get_by_role("button",name="DEMO-BROWSER-0102").click()
                            with page.expect_popup() as popup:
                                page.get_by_role("link",name="전체 페이지에서 열기").click()
                            incident_page=popup.value
                            try:
                                expect(incident_page.get_by_role("heading",name="위험 이력 상세")).to_be_visible()
                                expect(incident_page.get_by_text("조회 당시 상태",exact=False)).to_be_visible()
                                with incident_page.expect_response(lambda reply: "/api/incident?" in reply.url):
                                    incident_page.get_by_role("button",name="상세 다시 조회").click()
                                expect(incident_page.get_by_role("link",name="CSV 보고서")).to_be_visible()
                                expect(incident_page.get_by_text("Radio 혼잡 관측",exact=True).first).to_be_visible()
                            finally:
                                incident_page.close()
                            result["scenarios"][f"{width}x{height}-{theme}-detail-pages"]="pass"
                            cross_menu_search(page)
                            result["scenarios"][f"{width}x{height}-{theme}-global-search-client-detail"]="pass"
                            page.locator('[data-view="admin"]').click()
                            expect(page.locator("#search")).to_be_disabled()
                            expect(page.locator("#selected-history")).to_have_count(0)
                            require(verified_get(context,url,"/api/admin/selected-history?controller=WLC-1&command=D07&start=0&end=1",cert)[0]==403,
                                    "operator selected history endpoint not denied")
                            require(verified_get(context,url,"/api/admin/status",cert)[0]==403,
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
                                require(verified_get(context,url,"/api/radios?q=&page=1",cert)[0]==401,
                                        "logout did not revoke API access")
                            require(not errors and not external,"browser script error or external request")
                            result["scenarios"][f"{width}x{height}-{theme}"]="pass"
                        finally:
                            context.close()
                    context=browser.new_context()
                    try:
                        page=context.new_page()
                        # Derive the public deep link from the actual published
                        # fixture, not the obsolete pre-physical-ID string.
                        radio=view.page("DEMO-BROWSER-0001")
                        require(radio["total"]==1,"ambiguous deep-link fixture")
                        fragment="#radio/"+radio["rows"][0]["id"]
                        login(page,url,"admin",fragment)
                        expect(page.locator("#detailBody h3").first).to_contain_text("DEMO-BROWSER-0001")
                        page.get_by_role("link",name="목록으로 돌아가기").click()
                        page.locator('[data-view="admin"]').click()
                        page.get_by_role("button",name="관리자 상태 조회",exact=True).click()
                        expect(page.get_by_text("감사 기록",exact=False)).to_be_visible()
                        require(verified_get(context,url,"/api/admin/status",cert)[0]==200,
                                "admin endpoint unavailable")
                        history=page.locator("#selected-history")
                        expect(history.get_by_role("heading",name="정밀 수집 이력")).to_be_visible()
                        history.get_by_label("이력 WLC ID").fill("WLC-1")
                        history.get_by_label("정밀 이력 수집 항목").select_option("D07")
                        with page.expect_response(lambda r: "/api/admin/selected-history?" in r.url) as first:
                            history.get_by_role("button",name="정밀 이력 조회").click()
                        require(first.value.status==200,"selected history request failed")
                        expect(history.locator("tbody tr")).to_have_count(20)
                        expect(history.get_by_text("미검증 · 측정 시각/Counter Epoch/완전성 미확인",exact=True)).to_have_count(20)
                        expect(history).not_to_contain_text("PRIVATE-HISTORY")
                        expect(history).not_to_contain_text("a"*64)
                        with page.expect_response(lambda r: "/api/admin/selected-history?" in r.url) as second:
                            history.get_by_role("button",name="다음 이력").click()
                        require(second.value.status==200,"selected history cursor failed")
                        from urllib.parse import parse_qs
                        q1=parse_qs(urlsplit(first.value.url).query)
                        q2=parse_qs(urlsplit(second.value.url).query)
                        require(all(q1[k]==q2[k] for k in ("start","end","command","controller","limit")),
                                "selected history page changed query bounds")
                        require("after_time" in q2 and "after_id" in q2,"cursor missing")
                        expect(history.locator("tbody tr")).to_have_count(1)
                        expect(history.locator("tbody td").nth(2)).to_have_text("2")
                        history.get_by_text("포트 보고값 2행",exact=True).click()
                        expect(history.get_by_text("포트 0 · Admin enabled / Oper up",exact=True)).to_be_visible()
                        expect(history.get_by_text("속도: 2500 Mbps · Duplex: full",exact=True)).to_be_visible()
                        expect(history.get_by_text("TX packets: 0 · TX bytes: 18446744073709551615 · RX packets: 미확인 · RX bytes: 300",exact=True)).to_be_visible()
                        expect(history.get_by_text("포트 1 · Admin disabled / Oper down",exact=True)).to_be_visible()
                        expect(history.get_by_text("속도: 미확인 · Duplex: 미확인",exact=True)).to_be_visible()
                        expect(history).not_to_contain_text("PRIVATE-HISTORY")
                        expect(history).not_to_contain_text("a"*64)
                        expect(history.get_by_role("button",name="다음 이력")).to_be_disabled()
                        history.get_by_label("정밀 이력 수집 항목").select_option("D03")
                        expect(history.locator("tbody tr")).to_have_count(0)
                        history.get_by_role("button",name="정밀 이력 조회").click()
                        expect(history.get_by_text("Radio reset: 18446744073709551615",exact=True)).to_be_visible()
                        expect(history.get_by_text("TX timeout: 미확인",exact=True)).to_be_visible()
                        expect(history.get_by_text("RX CRC error: 0",exact=True)).to_be_visible()
                        expect(history.get_by_text("채널 Busy 4초 %: 42.5",exact=True)).to_be_visible()
                        expect(history.get_by_text("채널 Busy 64초 %: 0",exact=True)).to_be_visible()
                        expect(history).not_to_contain_text("PRIVATE-HISTORY")
                        expect(history).not_to_contain_text("b"*64)
                        history.get_by_label("정밀 이력 수집 항목").select_option("D04")
                        expect(history.locator("tbody tr")).to_have_count(0)
                        history.get_by_role("button",name="정밀 이력 조회").click()
                        history.get_by_text("채널 보고값 2행",exact=True).click()
                        expect(history.get_by_text("채널 36 · 대역 5GHz",exact=True)).to_be_visible()
                        expect(history.get_by_text("Retry: 18446744073709551615 · PHY error: 0 · MAC error: 미확인",exact=True)).to_be_visible()
                        expect(history.get_by_text("Noise 원문: 92*",exact=True)).to_be_visible()
                        expect(history.get_by_text("Util/Quality 보고값: 채널 8 / RX 3 / TX 2 / 확장 채널 1 / Quality 95",exact=True)).to_be_visible()
                        expect(history.get_by_text("Noise 원문: 미확인",exact=True)).to_be_visible()
                        expect(history).not_to_contain_text("PRIVATE-HISTORY")
                        expect(history).not_to_contain_text("c"*64)
                        history.get_by_label("정밀 이력 수집 항목").select_option("D08")
                        expect(history.locator("tbody tr")).to_have_count(0)
                        history.get_by_label("정밀 이력 조회 기간").select_option("3600")
                        history.get_by_role("button",name="정밀 이력 조회").click()
                        expect(history).to_contain_text("조회 범위에 저장 기록 없음")
                        history.get_by_label("정밀 이력 조회 기간").select_option("86400")
                        history.get_by_role("button",name="정밀 이력 조회").click()
                        history.get_by_text("ARM 보고값 1행",exact=True).click()
                        expect(history.get_by_text("wifi0 · PHY 5GHz",exact=True)).to_be_visible()
                        expect(history.get_by_text("Reason: OFF(R) · 채널: 44- → 0 · Power 원문: 미확인 → 0 · 보고된 상대 경과: 62초",exact=True)).to_be_visible()
                        expect(history.get_by_text("wifi1 · PHY 2.4GHz",exact=True)).to_be_visible()
                        expect(history.get_by_text("이벤트 표 미보고 · 변경 없음으로 판단 불가",exact=True)).to_be_visible()
                        expect(history).not_to_contain_text("PRIVATE-HISTORY")
                        expect(history).not_to_contain_text("d"*64)
                        history.get_by_label("이력 WLC ID").fill("not-configured")
                        history.get_by_role("button",name="정밀 이력 조회").click()
                        expect(history).to_contain_text("이력 조회 실패")
                        expect(history.locator("tbody tr")).to_have_count(0)
                        archive=view.selected_history
                        try:
                            view.selected_history=None
                            history.get_by_label("이력 WLC ID").fill("WLC-1")
                            history.get_by_role("button",name="정밀 이력 조회").click()
                            expect(history).to_contain_text("정밀 이력 보존 비활성")
                            expect(history.locator("tbody tr")).to_have_count(0)
                        finally:
                            view.selected_history=archive
                        result["scenarios"]["admin_selected_history_index"]="pass"
                        submit_form(page,url,"/auth/logout",page.get_by_role("button",name="로그아웃",exact=True))
                        expect(page.locator('input[name="username"]')).to_be_visible()
                        result["scenarios"]["admin_login_status_logout"]="pass"
                        client_id="c"*32
                        require(verified_get(context,url,"/api/client?id="+client_id,cert)[0]==401,
                                "client detail exposed without session")
                        page=context.new_page()
                        login(page,url,"operator","#client/"+client_id)
                        expect(page.locator("#detailBody").get_by_text("72%")).to_be_visible()
                        require(verified_get(context,url,"/api/association-history?id=&page=1",cert)[0]==403,
                                "operator may not consult protected client history")
                        view.publish({},client_views=[],client_enabled=False)
                        page.get_by_role("button",name="상세 다시 조회").click()
                        expect(page.locator("#detailBody")).to_contain_text("현재 승인된 단말 관측을 조회할 수 없습니다.")
                        expect(page.locator("#detailBody")).not_to_contain_text("72%")
                        require(verified_get(context,url,"/api/client?id="+client_id,cert)[0]==404,
                                "withdrawn client observation exposed")
                        result["scenarios"]["client_deep_link_role_and_withdrawal"]="pass"
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
