"""Explicit localhost demonstration. Production auth/HTTPS not implemented."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json, tempfile, threading, time
from .storage import Store
from .demo import seed, synthetic_rows

STATIC=Path(__file__).resolve().parent/"static"
def handler_for(store):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("Host","").split(":")[0] not in ("127.0.0.1","localhost"):
                self.send_error(403)
                return
            routes={"/":("index.html","text/html; charset=utf-8"),"/style.css":("style.css","text/css; charset=utf-8"),
                    "/app.js":("app.js","text/javascript; charset=utf-8")}
            if self.path=="/api/snapshot":
                body=json.dumps(store.snapshot(),ensure_ascii=False).encode()
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
        def log_message(self,*args):
            pass
    return Handler

def serve(port=8765, once=False):
    # An ephemeral directory makes it impossible to accidentally expose existing operational data.
    with tempfile.TemporaryDirectory(prefix="awqm-demo-") as root:
        store=Store(root)
        seed(store)
        if once:
            print(json.dumps({"mode":"demo","radios":len(store.snapshot()["radios"])}))
            return
        server=ThreadingHTTPServer(("127.0.0.1",port),handler_for(store))
        stopped=threading.Event()
        def refresh():
            while not stopped.wait(60):
                for row in synthetic_rows():
                    store.save(row)
        worker=threading.Thread(target=refresh,daemon=True)
        worker.start()
        print(f"AWQM synthetic demo: http://127.0.0.1:{port}",flush=True)
        try:
            server.serve_forever()
        finally:
            stopped.set()
            worker.join()
            server.server_close()
