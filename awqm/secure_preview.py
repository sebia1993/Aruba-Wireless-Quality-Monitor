"""Local HTTPS authenticated *synthetic* preview, separate from diagnostic data.

Only explicitly configured Aruba identity verification may perform SSH. No
collection or live dataset is exposed by this preview. Test injection avoids
all real device access. Production service/network policy and AAA approval
remain required.
"""
import argparse,html,json,ssl,threading,socket
from http.cookies import SimpleCookie,CookieError
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs,urlsplit
import tempfile
from .auth import Auth,ArubaVerifier,AuthDenied,AuthBusy
from .web import handler_for,STATIC
from .demo import seed
from .storage import Store

COOKIE="awqm_session"
def tls_context(cert,key):
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version=ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(cert),str(key))
    return context

class TLSServer(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,port,handler,context):
        self.context=context
        super().__init__(("127.0.0.1",port),handler)
    def get_request(self):
        conn,addr=super().get_request()
        try:
            conn.settimeout(5)
            conn=self.context.wrap_socket(conn,server_side=True)
            conn.settimeout(10)
            return conn,addr
        except (OSError,ssl.SSLError):
            conn.close();raise

def secure_handler(store,auth):
    if store.mode!="demo":raise ValueError("synthetic preview only")
    Base=handler_for(store)
    class Handler(Base):
        def _token(self):
            try:
                jar=SimpleCookie(self.headers.get("Cookie",""))
                return jar[COOKIE].value if COOKIE in jar else None
            except CookieError:return None
        def _origin(self):
            return self._local() and self.headers.get_all("Origin")==["https://"+self.headers["Host"]]
        def _send(self,code,body=b"",kind="text/html; charset=utf-8",headers=()):
            self.send_response(code);self.send_header("Content-Type",kind)
            self.send_header("Content-Length",str(len(body)))
            self.send_header("Cache-Control","no-store")
            self.send_header("X-Content-Type-Options","nosniff")
            # Browser navigation POSTs use the document's referrer policy
            # when serializing Origin. Retain the exact same-origin POST gate
            # and prevent document referrers from leaving this origin.
            policy="same-origin" if kind.split(";",1)[0]=="text/html" else "no-referrer"
            self.send_header("Referrer-Policy",policy)
            self.send_header("Content-Security-Policy","default-src 'self'; form-action 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'")
            for k,v in headers:self.send_header(k,v)
            self.end_headers()
            if body:self.wfile.write(body)
        def _login_page(self,status=200,message=""):
            body=("<!doctype html><html lang='ko'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>AWQM HTTPS 인증 검증</title><h1>AWQM HTTPS 인증 검증</h1>"
                "<p>기존 Aruba 개인 계정 · 모의 대시보드 전용 · 실제 운영 데이터 없음</p>"
                "<p>"+html.escape(message)+"</p><form method='post' action='/auth/login'>"
                "<p><label>Aruba ID <input name='username' autocomplete='username' required maxlength='128'></label></p>"
                "<p><label>비밀번호 <input name='password' type='password' autocomplete='current-password' required maxlength='1024'></label></p>"
                "<button type='submit'>로그인</button></form></html>").encode("utf-8")
            self._send(status,body)
        def _redirect(self,cookie):
            self._send(303,headers=[("Location","/"),("Set-Cookie",cookie)])
        def _form(self,fields):
            if self.headers.get("Content-Type","").split(";")[0]!="application/x-www-form-urlencoded":
                raise ValueError("invalid form type")
            n=int(self.headers.get("Content-Length","0"))
            if not 1<=n<=8192:raise ValueError("invalid form size")
            parsed=parse_qs(self.rfile.read(n).decode("utf-8"),strict_parsing=True,max_num_fields=len(fields),keep_blank_values=True)
            if set(parsed)!=fields or any(len(v)!=1 for v in parsed.values()):raise ValueError("invalid form")
            return {k:v[0] for k,v in parsed.items()}
        def do_GET(self):
            if not self._local():self._send(403);return
            # Background snapshot polling must not keep an idle session alive.
            touch=self.path=="/" or urlsplit(self.path).path.startswith("/api/history")
            try:s=auth.session(self._token(),touch=touch)
            except AuthDenied:
                if self.path=="/":self._login_page()
                else:self._json(401,{"error":"authentication_required"})
                return
            if self.path=="/":
                form=("<form action='/auth/logout' method='post'><input type='hidden' name='csrf' value='"+html.escape(s.csrf,quote=True)+
                      "'><button type='submit'>로그아웃</button></form><p>권한: "+html.escape(s.role)+"</p>")
                page=(STATIC/"index.html").read_text(encoding="utf-8").replace("</aside>",form+"</aside>")
                self._send(200,page.encode("utf-8"));return
            if self.path=="/api/admin/status":
                if s.role!="admin":self._json(403,{"error":"access_denied"});return
                self._json(200,{"mode":"auth-preview","field_validated":False,"production":False});return
            if self.path=="/auth/whoami":
                self._json(200,{"username":s.username,"role":s.role,"mode":"auth-preview"});return
            # Existing read-only synthetic routes; the demo job POST API is disabled.
            super().do_GET()
        def do_POST(self):
            if not self._origin():self._send(403);return
            try:
                if self.path=="/auth/login":
                    data=self._form({"username","password"})
                    try:token=auth.login(data["username"],data["password"])
                    finally:data.clear()
                    self._redirect(COOKIE+"="+token+"; Path=/; Secure; HttpOnly; SameSite=Strict")
                elif self.path=="/auth/logout":
                    data=self._form({"csrf"})
                    auth.logout(self._token(),data["csrf"])
                    self._redirect(COOKIE+"=; Max-Age=0; Path=/; Secure; HttpOnly; SameSite=Strict")
                else:self._send(404)
            except AuthBusy:self._login_page(429,"로그인 제한 또는 인증 처리 중")
            except AuthDenied:self._login_page(401,"인증 실패 또는 세션 만료")
            except (ValueError,TypeError,UnicodeError,TimeoutError):self._send(400)
        def log_message(self,*args):pass
    return Handler

def main():
    p=argparse.ArgumentParser(description="Local HTTPS Aruba authentication preview; synthetic dashboard only")
    p.add_argument("--config",required=True,type=Path)
    p.add_argument("--authorized",action="store_true")
    p.add_argument("--port",type=int,default=8443)
    args=p.parse_args()
    if not args.authorized:p.error("authorized credential verification must be explicit")
    if not 1024<=args.port<=65535:p.error("invalid port")
    try:
        if args.config.stat().st_size>128000:raise ValueError()
        cfg=json.loads(args.config.read_text(encoding="utf-8"))
        if set(cfg)!={"auth_host","known_hosts","cert","key","roles"}:raise ValueError()
        auth=Auth(cfg["roles"],ArubaVerifier(cfg["auth_host"],cfg["known_hosts"]))
        context=tls_context(cfg["cert"],cfg["key"])
    except (OSError,ValueError,TypeError,KeyError,ssl.SSLError):p.error("invalid protected auth configuration or certificate")
    with tempfile.TemporaryDirectory(prefix="awqm-auth-preview-") as root:
        s=Store(root);seed(s)
        server=TLSServer(args.port,secure_handler(s,auth),context)
        print("AWQM HTTPS auth preview: https://127.0.0.1:"+str(args.port)+" (synthetic only)",flush=True)
        try:server.serve_forever()
        finally:server.server_close()

if __name__=="__main__":main()
