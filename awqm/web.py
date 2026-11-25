from .collection_owner import CollectionBusy
"""Explicit localhost demonstration. Production auth/HTTPS not implemented."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import contextmanager
from pathlib import Path
import json, tempfile, threading, time, secrets, hmac, sqlite3
from .storage import Store
from .demo import seed, synthetic_rows, synthetic_clients
from urllib.parse import urlsplit,parse_qs
from .reporting import history_report,csv_report,html_report

STATIC=Path(__file__).resolve().parent/"static"
def handler_for(store,engine=None, *, settings_token=None, devices=None):
    token=secrets.token_urlsafe(32) if engine else None
    class Handler(BaseHTTPRequestHandler):
        def _local(self):
            host=self.headers.get("Host","")
            return self.headers.get_all("Host") == [host] and host in (f"127.0.0.1:{self.server.server_port}",f"localhost:{self.server.server_port}")
        def _json(self,status,value):
            body=json.dumps(value,ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type","application/json; charset=utf-8")
            self.send_header("Content-Length",str(len(body)))
            self.send_header("Cache-Control","no-store")
            self.send_header("X-Content-Type-Options","nosniff")
            self.end_headers();self.wfile.write(body)
        def do_GET(self):
            if not self._local():
                self.send_error(403)
                return
            path=urlsplit(self.path)
            if path.path in ("/api/history","/api/history.csv","/api/history.html"):
                try:
                    q=parse_qs(path.query,max_num_fields=4,strict_parsing=True)
                    if set(q)!={"radio_id","start","end","resolution"} or any(len(v)!=1 for v in q.values()):raise ValueError()
                    report=history_report(store,q["radio_id"][0],int(q["start"][0]),int(q["end"][0]),q["resolution"][0])
                    if path.path=="/api/history":
                        self._json(200,report);return
                    body=csv_report(report) if path.path.endswith(".csv") else html_report(report)
                    self.send_response(200)
                    self.send_header("Content-Type","text/csv; charset=utf-8" if path.path.endswith(".csv") else "text/html; charset=utf-8")
                    self.send_header("Content-Disposition",'attachment; filename="awqm-demo-history.'+("csv" if path.path.endswith(".csv") else "html")+'"')
                    self.send_header("Content-Length",str(len(body)))
                    self.send_header("Cache-Control","no-store")
                    self.send_header("X-Content-Type-Options","nosniff")
                    self.send_header("Content-Security-Policy","default-src 'none'; frame-ancestors 'none'")
                    self.end_headers();self.wfile.write(body);return
                except (ValueError,TypeError,OverflowError):
                    self._json(400,{"error":"invalid_history_request"});return
                except (sqlite3.Error,OSError):
                    self._json(503,{"error":"history_unavailable"});return
            routes={"/":("index.html","text/html; charset=utf-8"),"/style.css":("style.css","text/css; charset=utf-8"),
                    "/app.js":("app.js","text/javascript; charset=utf-8")}
            if devices is not None:
                routes.update({"/devices": ("devices.html", "text/html; charset=utf-8"),
                               "/devices.js": ("devices.js", "text/javascript; charset=utf-8"),
                               "/devices.css": ("devices.css", "text/css; charset=utf-8")})
            if self.path=="/api/snapshot":
                snapshot=store.snapshot()
                snapshot["clients"]=synthetic_clients(snapshot["radios"]) if engine is None else engine.client_views()
                if engine is not None:
                    snapshot["engine_demo"]=True
                    snapshot["engine"]=dict(engine.last_result)
                    snapshot["maintenance"]=getattr(engine,"last_maintenance",{})
                    snapshot["jobs"]=[dict(j,controller=k) for k in engine.workers for j in engine.job_status(k)]
                    snapshot["demo_token"]=token
                body=json.dumps(snapshot,ensure_ascii=False).encode()
                kind="application/json; charset=utf-8"
            elif self.path in routes:
                name,kind=routes[self.path]
                body=(STATIC/name).read_bytes()
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type",kind)
            self.send_header("Content-Length",str(len(body)))
            self.send_header("Cache-Control","no-store")
            self.send_header("X-Content-Type-Options","nosniff")
            self.send_header("Content-Security-Policy","default-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'none'")
            self.send_header("Referrer-Policy","no-referrer")
            self.end_headers()
            self.wfile.write(body)
        def do_POST(self):
            if self.path == "/api/desktop/devices":
                origin = "http://" + self.headers.get("Host", "")
                supplied = self.headers.get("X-AWQM-Settings", "")
                if (not self._local() or not settings_token or devices is None
                        or self.headers.get_all("Origin") != [origin]
                        or self.headers.get_all("X-AWQM-Settings") != [supplied]
                        or not supplied.isascii() or not hmac.compare_digest(supplied, settings_token)):
                    self._json(403, {"error": "현재 PC의 AWQM에서 웹 화면을 다시 여세요."})
                    return
                try:
                    lengths = self.headers.get_all("Content-Length", [])
                    if (len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit()
                            or self.headers.get_all("Transfer-Encoding")
                            or self.headers.get("Content-Type", "").split(";")[0] != "application/json"):
                        raise ValueError("요청 형식을 확인하세요.")
                    length = int(lengths[0])
                    if not 1 <= length <= 8192:
                        raise ValueError("요청 크기를 확인하세요.")
                    self.connection.settimeout(5)
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise ValueError("요청이 완전하지 않습니다.")
                    value = devices.dispatch(json.loads(raw))
                except CollectionBusy as exc:
                    self._json(409, {"reason": exc.reason, "error": "다른 AWQM 수집기 또는 SSH 종료 확인 문제가 있습니다. 모든 로컬 조회/운영 서비스를 중지하고 종료를 확인한 뒤 다시 시도하세요. 저장 요청의 등록 정보는 남을 수 있습니다."})
                    return
                except (ValueError, TypeError, KeyError) as error:
                    # Only explicitly raised validation errors are presented; never echo JSON or credentials.
                    message = str(error) if type(error) is ValueError else "장비 설정 요청을 확인하세요."
                    self._json(400, {"error": message[:160]})
                    return
                except (RuntimeError, OSError):
                    self._json(503, {"error": "장비 설정 또는 조회를 처리할 수 없습니다."})
                    return
                self._json(200, value)
                return
            if engine is None or not self._local():
                self.send_error(403);return
            origin=self.headers.get("Origin","")
            if origin!="http://"+self.headers["Host"] or not hmac.compare_digest(self.headers.get("X-AWQM-Demo",""),token):
                self.send_error(403);return
            if self.path not in ("/api/demo/jobs","/api/demo/jobs/cancel") or self.headers.get("Content-Type","").split(";")[0]!="application/json":
                self.send_error(400);return
            try:
                length=int(self.headers.get("Content-Length","0"))
                if not 1<=length<=4096:raise ValueError()
                data=json.loads(self.rfile.read(length))
                if not isinstance(data,dict):raise ValueError()
                controller=data["controller"]
                if controller not in engine.workers:raise ValueError()
                from dataclasses import asdict
                if self.path.endswith("/cancel"):
                    if set(data)!={"controller","job_id"}:raise ValueError()
                    j=engine.cancel(controller,data["job_id"])
                else:
                    if set(data)!={"controller","request_id","ap_name"}:raise ValueError()
                    # Only APs observed in this synthetic instance; no arbitrary CLI.
                    if not any(r["controller"]==controller and r["ap_name"]==data["ap_name"] for r in store.snapshot()["radios"]):
                        raise ValueError()
                    j=engine.submit(controller,data["request_id"],"show ap debug client-table",data["ap_name"])
                self._json(200,dict(asdict(j),controller=controller))
            except (ValueError,TypeError,KeyError):
                self._json(400,{"error":"invalid_demo_request"})
            except RuntimeError:
                self._json(409,{"error":"job_not_accepted"})
            except (sqlite3.Error,OSError):
                self._json(503,{"error":"job_storage_unavailable"})
        def log_message(self,*args):
            pass
    return Handler

@contextmanager
def demo_server(port, *, settings_token=None, devices=None):
    """Own an isolated synthetic store and its refresh worker for one listener."""
    with tempfile.TemporaryDirectory(prefix="awqm-demo-") as root:
        store=Store(root)
        seed(store)
        server=ThreadingHTTPServer(("127.0.0.1",port),handler_for(
            store, settings_token=settings_token, devices=devices))
        stopped=threading.Event()
        def refresh():
            while not stopped.wait(60):
                store.save_batch(synthetic_rows())
        worker=threading.Thread(target=refresh,daemon=True)
        worker.start()
        try:
            yield server
        finally:
            stopped.set()
            worker.join()
            server.server_close()


def serve(port=8765,once=False,open_browser=False):
    if once:
        with tempfile.TemporaryDirectory(prefix="awqm-demo-") as root:
            store=Store(root)
            seed(store)
            snap=store.snapshot()
            print(json.dumps({"mode":"demo","radios":len(snap["radios"]),
                              "clients":len(synthetic_clients(snap["radios"]))}))
        return
    with demo_server(port) as server:
        url=f"http://127.0.0.1:{server.server_port}"
        print(f"AWQM synthetic demo: {url}",flush=True)
        if open_browser:
            import webbrowser
            try:webbrowser.open(url)
            except Exception:print("Open the localhost URL above in your browser.",flush=True)
        server.serve_forever()
