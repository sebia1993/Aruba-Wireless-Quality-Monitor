"""Actual Chromium exercise of the localhost settings UI with a deterministic collector."""
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import threading

from playwright.sync_api import sync_playwright, expect
from awqm.device_profiles import Profiles
from awqm.device_web import DeviceWeb
from awqm.web import demo_server


class Monitor:
    def __init__(self):
        self.states = {}
        self.requests = []
        self.interval = 60

    def configure_interval(self, seconds):
        self.interval = seconds

    def state(self, id):
        return self.states.get(id, {})

    def start(self, profile, password, *, once=False):
        self.requests.append((profile.id, password, once))
        self.states[profile.id] = dict(profile=profile, running=not once, status="시험 관측 완료",
            connection="SSH 인증 성공", cli="조회 완료", observed_at=1700000000,
            tables={"Radio": (("채널",), [("36",), ("40",)])})

    def stop(self, id):
        self.states[id].update(running=False, status="중지됨")

    def forget(self, id):
        self.states.pop(id, None)

    def close(self):
        pass


@contextmanager
def server(root):
    devices = DeviceWeb(Profiles(root), Monitor())
    token = "browser-test-" + "a" * 40
    with demo_server(0, settings_token=token, devices=devices) as http:
        worker = threading.Thread(target=http.serve_forever, daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{http.server_port}/", token, devices
        finally:
            http.shutdown()
            worker.join()
            devices.close()


def verify(output):
    with tempfile.TemporaryDirectory() as root, server(root) as (base, token, devices), sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(viewport={"width":1280,"height":800})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base + "devices")
            expect(page.locator("#notice")).to_contain_text("Windows AWQM")
            page.goto(base + "#settings=" + token)
            page.locator("#device-settings").click()
            expect(page.locator("#heading")).to_have_text("종합 현황")
            expect(page.locator("#cards .kpi")).to_have_count(4)
            page.locator("#device-nav [data-view=settings]").click()
            page.locator("#host").fill("192.0.2.10")
            page.locator("#username").fill("fixture")
            page.locator("#password").fill("fixture-only-password")
            page.locator("#test").click()
            expect(page.locator("#heading")).to_have_text("실시간 관측")
            expect(page.locator("#observed")).to_contain_text("시험 관측 완료")
            page.locator("#device-nav [data-view=settings]").click()
            page.locator("#save").click()
            expect(page.locator("#notice")).to_contain_text("저장하고 수집")
            page.locator("#table").select_option("Radio")
            expect(page.locator("#readings tbody tr")).to_have_count(2)
            if devices.monitor.requests != [("WLC-1","fixture-only-password",True),("WLC-1","fixture-only-password",False)]:
                raise RuntimeError("Browser did not reach the expected collector actions")
            page.close()
            if not devices.monitor.state("WLC-1")["running"]:
                raise RuntimeError("Closing the browser stopped the collector")
            page = browser.new_page(viewport={"width":390,"height":844})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base + "devices#settings=" + token)
            expect(page.locator("#cards .kpi")).to_have_count(4)
            page.locator("#device-nav [data-view=settings]").click()
            page.locator("#slot").select_option("WLC-2")
            expect(page.locator("#username")).to_have_value("fixture")
            expect(page.locator("#password")).to_have_value("")
            page.locator("#device-nav [data-view=operations]").click()
            page.locator("#interval").fill("120")
            page.locator("#save-interval").click()
            expect(page.locator("#notice")).to_contain_text("수집 주기를 저장")
            if devices.profiles.poll_interval() != 120 or devices.monitor.interval != 120:
                raise RuntimeError("Interval was not applied and persisted")
            page.locator("#device-nav [data-view=settings]").click()
            page.locator("#slot").select_option("WLC-1")
            page.locator("#stop").click()
            expect(page.locator("#notice")).to_contain_text("조회 중지")
            if devices.monitor.state("WLC-1")["running"]:
                raise RuntimeError("Stop did not reach collector")
            page.on("dialog", lambda dialog: dialog.accept())
            page.locator("#remove").click()
            expect(page.locator("#notice")).to_contain_text("삭제")
            if devices.profiles.list() or errors:
                raise RuntimeError("Profile removal or browser execution failed")
            if page.evaluate("document.documentElement.scrollWidth > innerWidth"):
                raise RuntimeError("Mobile layout overflows")
            result = {"status":"pass","desktop_and_mobile":True,"direct_web_registration":True,
                      "browser_close_keeps_collection":True,"account_reuse":True,
                      "password_not_returned_to_form":True,"persisted_interval":True,"stop_and_remove":True}
            output.write_text(json.dumps(result, indent=2), encoding="utf-8")
            print(json.dumps(result))
        finally:
            browser.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    verify(args.output)
