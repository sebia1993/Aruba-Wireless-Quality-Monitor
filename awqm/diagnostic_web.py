"""Opt-in HTTPS view for the read-only diagnostic Windows service.

The listener accepts only configured source networks and a fixed Host header.
Its bounded request pool never starts collection commands. The sole SSH
operation is coordinated, temporary personal-credential verification.
"""
import html,hmac,ipaddress,json,ssl,sqlite3,threading,time,re
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from http.cookies import SimpleCookie,CookieError
from urllib.parse import urlsplit,parse_qs
from .auth import Auth,AuthBusy,AuthDenied,ArubaVerifier
from .secure_preview import tls_context
from .reporting import csv_report,html_report
from .web import STATIC
from .audit import AuditLog
from .incidents import ReviewConflict
from .incident_reports import summary,incident_csv,incident_html
from .manual_jobs import ManualReservations,ReservationRejected
from .operations import OperationConflict,OperationRejected
from .notifications import Notifications,Busy as NotificationBusy
from wsproto.utilities import LocalProtocolError,RemoteProtocolError
from .http_bounds import ConnectionDeadline,HeaderBudget

COOKIE="__Host-awqm_session"

class CoordinatedVerifier:
    def __init__(self,worker,gate,verify,stop,operations=None):
        self.worker=worker;self.gate=gate;self.verify=verify;self.stop=stop;self.operations=operations
    def __call__(self,user,password):
        w=self.worker
        if self.stop.is_set() or getattr(w,"maintenance_requested",False) or not w.lock.acquire(blocking=False):
            raise AuthBusy("controller busy")
        try:
            if self.operations is not None:
                try:paused=self.operations.paused(w.controller)
                except (OSError,sqlite3.Error,RuntimeError,ValueError):raise AuthBusy("operation state unavailable") from None
                if paused:raise AuthBusy("controller paused")
            p=w.schedule.protection
            now=w.clock()
            if (w.session is None or w.blocked or w.schedule.paused or p.effective(now)!="normal"
                or p.last_cpu is None or p.last_cpu>=70 or p.budget(now,300)>=120):
                raise AuthBusy("controller not available for authentication")
            if not self.gate.lock.acquire(blocking=False):
                raise AuthBusy("authentication gate busy")
            try:
                if self.stop.is_set():raise AuthBusy("service stopping")
                # Account temporary authentication time against the same
                # controller's CLI budget to avoid invisible load.
                started=w.clock()
                try:
                    return self.verify(user,password)
                finally:
                    p.account(started,w.clock())
            finally:self.gate.lock.release()
        finally:w.lock.release()

class RestrictedTLS(ThreadingHTTPServer):
    daemon_threads=False
    block_on_close=True
    request_queue_size=32
    def __init__(self,cfg,handler,context,max_connections=24):
        self.cfg=cfg;self.context=context;self.capacity=threading.BoundedSemaphore(max_connections)
        super().__init__((cfg.bind,cfg.port),handler)
    def verify_request(self,request,address):
        try:addr=ipaddress.IPv4Address(address[0])
        except ValueError:return False
        return time.time()<self.cfg.expires_at and any(addr in n for n in self.cfg.networks)
    def process_request(self,request,client_address):
        if not self.capacity.acquire(blocking=False):
            self.shutdown_request(request);return
        try:super().process_request(request,client_address)
        except BaseException:
            self.capacity.release();raise
    def process_request_thread(self,request,client_address):
        conn=None
        try:
            request.settimeout(5)
            conn=self.context.wrap_socket(request,server_side=True)
            conn.settimeout(10)
            self.finish_request(conn,client_address)
        except (OSError,ssl.SSLError,ValueError):pass
        except Exception:
            # Avoid logging request bodies, identities, tokens or exception text.
            pass
        finally:
            try:self.shutdown_request(conn if conn is not None else request)
            finally:self.capacity.release()

def handler_for(view,auth,audit,cfg,manual=None,operations=None,stop=None,http_deadline=30):
    notifications=Notifications(view,auth,cfg,stop)
    class Handler(BaseHTTPRequestHandler):
        protocol_version="HTTP/1.0"
        def setup(self):
            super().setup()
            self.http_deadline=ConnectionDeadline(self.connection,http_deadline)
        def finish(self):
            if hasattr(self,"http_deadline"):self.http_deadline.disarm()
            super().finish()
        def parse_request(self):
            # Request line and total header bytes are independently bounded.
            if len(self.raw_requestline)>8192:
                self.requestline="";self.request_version="HTTP/1.0";self.command=""
                self.close_connection=True
                self.send_error(414,"Request line too long")
                return False
            original=self.rfile
            self.rfile=HeaderBudget(original)
            try:ok=super().parse_request()
            finally:self.rfile=original
            if ok and getattr(self.headers,"defects",()):
                self.send_error(400,"Malformed headers");return False
            return ok
        def log_message(self,*a):pass
        def _local(self):
            # No proxy trust or X-Forwarded-For. TLS name and port are fixed.
            return self.headers.get_all("Host")==[cfg.authority] and time.time()<cfg.expires_at
        def _token(self):
            try:
                headers=self.headers.get_all("Cookie",[])
                if len(headers)!=1:return None
                jar=SimpleCookie(headers[0])
                return jar[COOKIE].value if COOKIE in jar else None
            except (CookieError,ValueError):return None
        def _send(self,status,body=b"",kind="text/plain; charset=utf-8",headers=()):
            self.send_response(status)
            self.send_header("Content-Type",kind)
            self.send_header("Content-Length",str(len(body)))
            self.send_header("Cache-Control","no-store")
            self.send_header("X-Content-Type-Options","nosniff")
            # Browser navigation POSTs use the document's referrer policy
            # when serializing Origin. Retain the exact same-origin POST gate
            # and prevent document referrers from leaving this origin.
            policy="same-origin" if kind.split(";",1)[0]=="text/html" else "no-referrer"
            self.send_header("Referrer-Policy",policy)
            self.send_header("X-Frame-Options","DENY")
            self.send_header("Content-Security-Policy","default-src 'self'; connect-src 'self' wss://"+cfg.authority+"; form-action 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'")
            self.send_header("Connection","close")
            for k,v in headers:self.send_header(k,v)
            self.end_headers()
            if body:self.wfile.write(body)
        def _json(self,status,value):
            self._send(status,json.dumps(value,ensure_ascii=False,allow_nan=False).encode("utf-8"),"application/json; charset=utf-8")
        def _login(self,status=200,message=""):
            page=("<!doctype html><html lang='ko'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>AWQM 진단 로그인</title><link rel='stylesheet' href='/diagnostic.css'>"
                "<body class='login'><main><h1>AWQM</h1><p>기존 Aruba 개인 계정 · 현장 검증 전 진단 모드</p>"
                "<p>"+html.escape(message)+"</p><form action='/auth/login' method='post'>"
                "<label>Aruba ID<input name='username' required maxlength='128' autocomplete='username'></label>"
                "<label>비밀번호<input name='password' type='password' required maxlength='1024' autocomplete='current-password'></label>"
                "<button type='submit'>로그인</button></form></main>"
                "<script src='/login-detail.js' defer></script></body></html>")
            self._send(status,page.encode("utf-8"),"text/html; charset=utf-8")
        def _redirect(self,cookie):
            self._send(303,headers=(("Location","/"),("Set-Cookie",cookie)))
        def _form(self,fields):
            types=self.headers.get_all("Content-Type",[])
            if self.headers.get_all("Transfer-Encoding") or len(types)!=1 or types[0].split(";")[0]!="application/x-www-form-urlencoded":
                raise ValueError("invalid form")
            lengths=self.headers.get_all("Content-Length",[])
            if len(lengths)!=1 or not re.fullmatch(r"[0-9]{1,5}",lengths[0]):raise ValueError("invalid form length")
            n=int(lengths[0])
            if not 1<=n<=8192:raise ValueError("invalid form length")
            raw=self.rfile.read(n)
            if len(raw)!=n:raise ValueError("truncated form")
            q=parse_qs(raw.decode("utf-8"),strict_parsing=True,keep_blank_values=True,max_num_fields=len(fields))
            if set(q)!=fields or any(len(v)!=1 for v in q.values()):raise ValueError("invalid form")
            return {k:v[0] for k,v in q.items()}
        def _query(self,path,fields,optional=()):
            allowed=set(fields)|set(optional)
            q=parse_qs(path.query,strict_parsing=True,max_num_fields=len(allowed),keep_blank_values=True)
            if not set(fields)<=set(q)<=allowed or any(len(v)!=1 for v in q.values()):raise ValueError("invalid query")
            return {k:v[0] for k,v in q.items()}
        def _notifications(self):
            token=self._token()
            try:
                auth.session(token,touch=False)
                ws,reply=notifications.handshake(self.headers,self.request_version,self.path)
            except AuthDenied:self._send(401);return
            except PermissionError:self._send(403);return
            except (ValueError,LocalProtocolError,RemoteProtocolError):self._send(400);return
            try:
                with notifications.slot(token):
                    if not self.http_deadline.disarm():return
                    self.close_connection=True
                    # From here onward only WebSocket bytes may be written.
                    try:notifications.run(self.connection,ws,reply,token)
                    except (AuthDenied,OSError,ValueError,LocalProtocolError,RemoteProtocolError):pass
            except NotificationBusy:self._send(503)
        def do_GET(self):
            if not self._local():self._send(403);return
            if self.headers.get_all("Transfer-Encoding") or self.headers.get_all("Content-Length",[]) not in ([],["0"]):
                self._send(400);return
            path=urlsplit(self.path)
            if path.path=="/ws/updates":self._notifications();return
            # Public login assets contain no telemetry or session data.
            if path.path=="/diagnostic.css" and not path.query:
                self._send(200,(STATIC/"diagnostic.css").read_bytes(),"text/css; charset=utf-8");return
            if self.path=="/login-detail.js":
                self._send(200,(STATIC/"login-detail.js").read_bytes(),"text/javascript; charset=utf-8");return
            try:
                s=auth.session(self._token(),touch=path.path in ("/","/api/history","/api/history.csv","/api/history.html","/api/admin/status","/api/incidents","/api/incident","/api/incident/summary","/api/incident/report.csv","/api/incident/report.html","/api/neighbors","/api/association-history","/api/admin/collection"))
            except AuthDenied:
                if self.path=="/":self._login()
                else:self._json(401,{"error":"authentication_required"})
                return
            try:
                if self.path=="/":
                    page=(STATIC/"diagnostic.html").read_text(encoding="utf-8")
                    page=page.replace("{{ROLE}}",html.escape(s.role)).replace("{{CSRF}}",html.escape(s.csrf,quote=True))
                    self._send(200,page.encode("utf-8"),"text/html; charset=utf-8")
                elif self.path=="/diagnostic.js":
                    self._send(200,(STATIC/"diagnostic.js").read_bytes(),"text/javascript; charset=utf-8")
                elif self.path=="/api/publication":
                    self._json(200,view.notification())
                elif path.path=="/api/radios":
                    q=self._query(path,{"q","page"})
                    self._json(200,view.page(q["q"],int(q["page"])))
                elif path.path=="/api/radio":
                    q=self._query(path,{"id"})
                    if not re.fullmatch(r"[0-9a-f]{32}",q["id"]):
                        raise ValueError("invalid Radio")
                    rows=view.page(size=1,token=q["id"])["rows"]
                    if rows:self._json(200,rows[0])
                    else:self._json(404,{"error":"unknown_radio"})
                elif path.path in ("/api/history","/api/history.csv","/api/history.html"):
                    q=self._query(path,{"id","start","end"},optional=("resolution",))
                    report=view.history(q["id"],int(q["start"]),int(q["end"]),q.get("resolution","raw"))
                    event={"/api/history":"history","/api/history.csv":"export_csv","/api/history.html":"export_html"}[path.path]
                    audit.record(s.username,event,q["id"])
                    if event=="history":self._json(200,report)
                    else:
                        fmt="csv" if event=="export_csv" else "html"
                        body=csv_report(report) if fmt=="csv" else html_report(report)
                        self._send(200,body,"text/csv; charset=utf-8" if fmt=="csv" else "text/html; charset=utf-8",
                            (("Content-Disposition",'attachment; filename="awqm-diagnostic-history.'+fmt+'"'),))
                elif self.path=="/api/manual-jobs":
                    self._json(200,manual.list(s.username) if manual is not None else {"enabled":False,"epoch":None,"rows":[],"total":0})
                elif path.path=="/api/neighbors":
                    q=self._query(path,{"id"})
                    result=view.neighbors(q["id"])
                    audit.record(s.username,"neighbor_view",q["id"])
                    self._json(200,result)
                elif path.path=="/api/association-history":
                    if s.role!="admin":self._json(403,{"error":"access_denied"});return
                    q=self._query(path,{"id","page"})
                    audit.record(s.username,"association_history")
                    self._json(200,view.association_page(q["id"],int(q["page"])))
                elif path.path=="/api/clients":
                    q=self._query(path,{"q","page"})
                    # Log detailed-client view access without raw client identifiers.
                    audit.record(s.username,"client_view")
                    self._json(200,view.clients_page(q["q"],int(q["page"])))
                elif path.path=="/api/incidents":
                    q=self._query(path,{"page","status","start","end"})
                    self._json(200,view.incident_list(int(q["page"]),q["status"],int(q["start"]),int(q["end"])))
                elif path.path in ("/api/incident","/api/incident/summary","/api/incident/report.csv","/api/incident/report.html"):
                    q=self._query(path,{"id"})
                    detail=view.incident_detail(q["id"])
                    event={"/api/incident":"incident_detail","/api/incident/summary":"incident_summary",
                           "/api/incident/report.csv":"incident_csv","/api/incident/report.html":"incident_html"}[path.path]
                    audit.record(s.username,event,q["id"])
                    if event=="incident_detail":self._json(200,detail)
                    elif event=="incident_summary":self._json(200,{"text":summary(detail),"service_impact":"unverified"})
                    else:
                        fmt="csv" if event=="incident_csv" else "html"
                        body=incident_csv(detail) if fmt=="csv" else incident_html(detail)
                        self._send(200,body,"text/csv; charset=utf-8" if fmt=="csv" else "text/html; charset=utf-8",
                            (("Content-Disposition",'attachment; filename="awqm-incident-report.'+fmt+'"'),))
                elif self.path=="/api/admin/collection":
                    if s.role!="admin":self._json(403,{"error":"access_denied"});return
                    if operations is None:self._json(200,{"enabled":False});return
                    self._json(200,{"enabled":True,"desired":operations.snapshot(),"applied":view.operation_status(),
                                    "recent":operations.recent()})
                elif self.path=="/api/admin/status":
                    if s.role!="admin":self._json(403,{"error":"access_denied"});return
                    audit.record(s.username,"admin_status")
                    remaining=max(0,(cfg.expires_at-int(time.time()))//86400)
                    self._json(200,{"audit":audit.summary(),"certificate_days":remaining,
                        "certificate_warning":remaining<=30,"field_validated":False,
                        "automatic_detail":view.client_enabled,"raw_client_identity_storage":False,"pseudonymous_association_history":view.association_history is not None})
                else:self._send(404)
            except (ValueError,TypeError,OverflowError):self._json(400,{"error":"invalid_request"})
            except (RuntimeError,sqlite3.Error,OSError):self._json(503,{"error":"data_or_audit_unavailable"})
        def do_POST(self):
            if not self._local() or self.headers.get_all("Origin")!=["https://"+cfg.authority]:
                self._send(403);return
            try:
                if self.path=="/auth/login":
                    d=self._form({"username","password"})
                    try:token=auth.login(d["username"],d["password"])
                    finally:d.clear()
                    self._redirect(COOKIE+"="+token+"; Path=/; Secure; HttpOnly; SameSite=Strict")
                elif self.path=="/auth/logout":
                    d=self._form({"csrf"})
                    auth.logout(self._token(),d["csrf"])
                    self._redirect(COOKIE+"=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict")
                elif self.path=="/api/admin/collection":
                    s=auth.session(self._token())
                    if s.role!="admin":self._json(403,{"error":"access_denied"});return
                    d=self._form({"csrf","target","action","revision"})
                    if not hmac.compare_digest(d["csrf"],s.csrf):raise AuthDenied("invalid csrf")
                    if operations is None:raise OperationRejected("operation_control_unavailable")
                    if d["action"] not in ("pause","resume"):raise OperationRejected("invalid_operation")
                    result=operations.set("web:"+s.username,d["target"],d["action"]=="pause",int(d["revision"]))
                    self._json(200,result)
                elif self.path in ("/api/manual-jobs/reserve","/api/manual-jobs/cancel"):
                    s=auth.session(self._token())
                    if s.role not in ("operator","admin"):raise AuthDenied("role denied")
                    reserve=self.path.endswith("/reserve")
                    d=self._form({"csrf","epoch","request_id","id"} if reserve else {"csrf","epoch","receipt_id"})
                    if not hmac.compare_digest(d["csrf"],s.csrf):raise AuthDenied("invalid csrf")
                    if manual is None:raise ReservationRejected("manual_detail_disabled")
                    if reserve:self._json(200,manual.reserve(s.username,d["epoch"],d["request_id"],d["id"]))
                    else:self._json(200,manual.cancel(s.username,d["epoch"],d["receipt_id"]))
                elif self.path=="/api/incident/review":
                    s=auth.session(self._token())
                    if s.role not in ("operator","admin"):raise AuthDenied("role denied")
                    d=self._form({"csrf","id","revision","request_id","verdict","reason","candidate"})
                    if not hmac.compare_digest(d["csrf"],s.csrf):raise AuthDenied("invalid csrf")
                    # The durable review itself records actor, exact evidence,
                    # decision and timestamp in one core transaction.
                    self._json(200,view.incident_review(d["id"],int(d["revision"]),d["request_id"],
                        s.username,d["verdict"],d["reason"],d["candidate"]))
                else:self._send(404)
            except (OperationConflict,OperationRejected) as exc:self._json(409,{"error":str(exc)})
            except ReservationRejected as exc:self._json(409,{"error":str(exc)})
            except ReviewConflict:self._json(409,{"error":"evidence_revision_unavailable"})
            except AuthBusy:self._login(429,"인증 처리 중 또는 보호 정책에 따라 잠시 대기합니다.")
            except AuthDenied:self._login(401,"인증 실패 또는 세션 만료")
            except (ValueError,TypeError,UnicodeError,TimeoutError):self._send(400)
            except (RuntimeError,sqlite3.Error,OSError):self._send(503)
    return Handler

def create_listener(cfg,service_cfg,workers,gate,view,stop,manual_enabled=False,operations=None):
    worker=workers[cfg.controller]
    target=next(t for t in service_cfg.targets if t.id==cfg.controller)
    verifier=CoordinatedVerifier(worker,gate,ArubaVerifier(target.ip,service_cfg.known_hosts),stop,operations=operations)
    audit=AuditLog(service_cfg.root/"logs"/"web-audit.db")
    # Conservative defaults; actual AAA lockout threshold is site-specific.
    auth=Auth(cfg.roles,verifier,user_limit=2,global_limit=20,window=300,audit_sink=audit.auth_event)
    manual=ManualReservations(view,workers,audit,stop,operations=operations) if manual_enabled else None
    return RestrictedTLS(cfg,handler_for(view,auth,audit,cfg,manual,operations,stop),tls_context(cfg.cert,cfg.key))
