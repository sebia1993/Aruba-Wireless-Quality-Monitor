"""Explicit localhost demonstration. Production auth/HTTPS not implemented."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json, tempfile, threading, time, secrets, hmac, sqlite3
from .storage import Store
from .demo import seed, synthetic_rows, synthetic_clients

STATIC=Path(__file__).resolve().parent/"static"
def handler_for(store,engine=None):
    token=secrets.token_urlsafe(32) if engine else None
    class Handler(BaseHTTPRequestHandler):
        def _local(self):
            host=self.headers.get("Host","")
            return host in (f"127.0.0.1:{self.server.server_port}",f"localhost:{self.server.server_port}")
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
            routes={"/":("index.html","text/html; charset=utf-8"),"/style.css":("style.css","text/css; charset=utf-8"),
                    "/app.js":("app.js","text/javascript; charset=utf-8")}
            if self.path=="/api/snapshot":
                snapshot=store.snapshot()
                snapshot["clients"]=synthetic_clients(snapshot["radios"]) if engine is None else []
                if engine is not None:
                    snapshot["engine_demo"]=True
                    snapshot["engine"]=dict(engine.last_result)
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
            self.send_header("Content-Security-Policy","default-src 'self'; frame-ancestors 'none'; object-src 'none'")
            self.end_headers()
            self.wfile.write(body)
        def do_POST(self):
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

def serve(port=8765, once=False):
    # An ephemeral directory makes it impossible to accidentally expose existing operational data.
    with tempfile.TemporaryDirectory(prefix="awqm-demo-") as root:
        store=Store(root)
        seed(store)
        if once:
            snap=store.snapshot()
            print(json.dumps({"mode":"demo","radios":len(snap["radios"]),
                              "clients":len(synthetic_clients(snap["radios"]))}))
            return
        server=ThreadingHTTPServer(("127.0.0.1",port),handler_for(store))
        stopped=threading.Event()
        def refresh():
            while not stopped.wait(60):
                store.save_batch(synthetic_rows())
        worker=threading.Thread(target=refresh,daemon=True)
        worker.start()
        print(f"AWQM synthetic demo: http://127.0.0.1:{port}",flush=True)
        try:
            server.serve_forever()
        finally:
            stopped.set()
            worker.join()
            server.server_close()
