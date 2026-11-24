"""Real loopback TLS/WSS synthetic capacity check, never connects to a WLC.

Uses the existing full-size CLI fixture ingestion first. Ten authenticated
fixture users each hold a WSS connection while issuing HTTPS GET requests.
Measures transport/server latency, not DOM/browser rendering or real AAA.
"""
import concurrent.futures,json,socket,ssl,tempfile,threading,time
from collections import deque
from datetime import datetime,timedelta,timezone
from http.client import HTTPSConnection
from ipaddress import IPv4Address,IPv4Network
from pathlib import Path
from types import SimpleNamespace
from wsproto import WSConnection,ConnectionType
from wsproto.events import Request,AcceptConnection,RejectConnection,TextMessage,Ping,CloseConnection
from .auth import Auth
from .audit import AuditLog
from .diagnostic_view import DiagnosticView
from .diagnostic_web import RestrictedTLS,handler_for
from .maintenance import write_json
from .secure_preview import tls_context
from .storage import Store
from .web_config import WebConfig,cert_expiry

def certificate(root):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization,hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,"AWQM synthetic loopback validation")])
    now=datetime.now(timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5))
        .not_valid_after(now+timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(IPv4Address("127.0.0.1"))]),False)
        .sign(key,hashes.SHA256()))
    p=root/"fixture-cert.pem";k=root/"fixture-key.pem"
    p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    k.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    return p,k

class FixtureSocket:
    def __init__(self,authority,port,trust,token,origin=None):
        self.ws=WSConnection(ConnectionType.CLIENT);self.pending=deque();self.socket=None
        raw=socket.create_connection(("127.0.0.1",port),timeout=5)
        try:
            self.socket=trust.wrap_socket(raw,server_hostname="127.0.0.1")
            self.socket.sendall(self.ws.send(Request(host=authority,target="/ws/updates",extra_headers=[
                (b"origin",(origin or "https://"+authority).encode("ascii")),
                (b"cookie",("__Host-awqm_session="+token).encode("ascii"))])))
        except BaseException:
            if self.socket:self.socket.close()
            else:raw.close()
            raise
    def event(self,kinds,timeout=5):
        until=time.monotonic()+timeout
        while time.monotonic()<until:
            while self.pending:
                event=self.pending.popleft()
                if isinstance(event,Ping):
                    self.socket.sendall(self.ws.send(event.response()));continue
                if isinstance(event,kinds):return event
                if isinstance(event,CloseConnection):raise RuntimeError("unexpected websocket close")
            self.socket.settimeout(max(.1,until-time.monotonic()))
            data=self.socket.recv(4096)
            if not data:break
            self.ws.receive_data(data);self.pending.extend(self.ws.events())
        raise RuntimeError("expected websocket event missing")
    def publication(self,revision):
        until=time.monotonic()+5;parts=[];size=0
        while True:
            event=self.event((TextMessage,),timeout=max(.1,until-time.monotonic()))
            size+=len(event.data)
            if size>256 or time.monotonic()>until:raise RuntimeError("unexpected publication framing")
            parts.append(event.data)
            if event.message_finished:break
        d=json.loads("".join(parts))
        if d!={"type":"publication","revision":revision,"available":True}:raise RuntimeError("publication mismatch")
        return d
    def close(self):
        if self.socket is not None:
            self.socket.close();self.socket=None

def https_test(root,aps=500,clients=10):
    from .scale_validation import scale_test,p95
    root=Path(root);root.mkdir(parents=True,exist_ok=False)
    initial=scale_test(root/"fixture",aps,clients)
    if initial["result"]!="pass":raise RuntimeError("synthetic ingestion prerequisite failed")
    store=Store(root/"fixture/data",mode="diagnostic")
    snapshot=store.snapshot()
    expected=aps*8
    if len(snapshot["radios"])!=expected or snapshot["pending_writes"]:
        raise RuntimeError("synthetic coverage mismatch")
    current=[(r["radio_id"],r["observed_at"]) for r in snapshot["radios"]]
    view=DiagnosticView(store,time.time)
    meta={"coherent_controllers":4,"expected_controllers":4,"mapped_radios":expected,"active_clients":aps*4*clients}
    roles={f"fixture-{i}":"operator" for i in range(11)}
    # No real identities or external verifier. Session setup excluded from
    # measured API latency; production AAA timing is a distinct test.
    auth=Auth(roles,lambda u,p:p=="synthetic-fixture",global_limit=12)
    tokens=[auth.login(u,"synthetic-fixture") for u in roles]
    sockets=[];server=None;thread=None;stop=threading.Event()
    with tempfile.TemporaryDirectory(prefix="awqm-loopback-") as tmp:
        p=Path(tmp);cert,key=certificate(p);audit=AuditLog(p/"audit.db")
        cfg=WebConfig("127.0.0.1","127.0.0.1:0",0,(IPv4Network("127.0.0.1/32"),),
            "fixture",cert,key,roles,cert_expiry(cert,"127.0.0.1"))
        cfg=SimpleNamespace(**vars(cfg))
        try:
            server=RestrictedTLS(cfg,handler_for(view,auth,audit,cfg,stop=stop),tls_context(cert,key))
            port=server.server_port;cfg.authority=f"127.0.0.1:{port}"
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            trust=ssl.create_default_context(cafile=str(cert))
            view.publish(meta,current)
            for token in tokens[:10]:
                s=FixtureSocket(cfg.authority,port,trust,token);sockets.append(s)
                s.event((AcceptConnection,));s.publication(1)
            # Actual network-level capacity and same-origin enforcement.
            for token,origin,expected_status in ((tokens[10],None,503),(tokens[0],None,503),
                    (tokens[10],"https://invalid.example",403)):
                s=FixtureSocket(cfg.authority,port,trust,token,origin)
                try:
                    reply=s.event((RejectConnection,))
                    if reply.status_code!=expected_status:raise RuntimeError("websocket admission mismatch")
                finally:s.close()
            def get(i,path):
                conn=HTTPSConnection("127.0.0.1",port,context=trust,timeout=5)
                try:
                    conn.request("GET",path,headers={"Cookie":"__Host-awqm_session="+tokens[i]})
                    response=conn.getresponse();raw=response.read(2*1024*1024+1)
                    if len(raw)>2*1024*1024:raise RuntimeError("response exceeds validation bound")
                    return response.status,raw
                finally:conn.close()
            # Refresh metadata before timing a new notification. All socket
            # readers and HTTPS clients run concurrently; no simulated clock.
            start=time.perf_counter();view.publish(meta,current)
            def websocket_reader(i):
                sockets[i].publication(2)
                return time.perf_counter()-start
            def https_reader(i):
                times=[]
                for j in range(5):
                    path="/api/radios?q=&page=1" if (i+j)%2==0 else "/api/radios?q=DEMO-AP-0001&page=1"
                    before=time.perf_counter();status,raw=get(i,path);times.append(time.perf_counter()-before)
                    if status!=200:raise RuntimeError("HTTPS read failed")
                    data=json.loads(raw)
                    if (not data["available"] or data["total"]!=(expected if (i+j)%2==0 else 2)
                        or data["active_clients"]!=aps*4*clients or len(data["rows"])!=min(data["total"],100)):
                        raise RuntimeError("HTTPS coverage mismatch")
                    if any(r.get("quality_score") is not None or len(r["id"])!=32 for r in data["rows"]):
                        raise RuntimeError("unexpected diagnostic provenance or identity")
                return times
            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
                ws_f=[pool.submit(websocket_reader,i) for i in range(10)]
                https_f=[pool.submit(https_reader,i) for i in range(10)]
                ws_times=[f.result(timeout=15) for f in ws_f]
                times=[t for f in https_f for t in f.result(timeout=30)]
            auth.logout(tokens[0],auth.session(tokens[0],touch=False).csrf)
            if sockets[0].event((CloseConnection,)).code!=1008 or get(0,"/api/radios?q=&page=1")[0]!=401:
                raise RuntimeError("session revocation failure")
            report={"kind":"synthetic_loopback_https_wss_capacity","version":__import__("awqm").__version__,
                "radio_count":expected,"ap_count":aps*4,"client_count":aps*4*clients,
                "authenticated_clients":10,"https_requests":len(times),"wss_notifications":len(ws_times),
                "https_p95_seconds":p95(times),"https_max_seconds":round(max(times),6),
                "wss_p95_seconds":p95(ws_times),"wss_max_seconds":round(max(ws_times),6),
                "admission_and_revocation":"pass",
                "result":"pass" if p95(times)<=2 and max(ws_times)<=2 else "fail",
                "actual_tls_transport":True,"browser_rendering_validated":False,
                "real_aaa_validated":False,"real_device_validated":False,"soak_validated":False}
            write_json(root/"https-result.json",report)
            return report
        finally:
            stop.set()
            for s in sockets:s.close()
            if server is not None:
                if thread is not None and thread.is_alive():server.shutdown()
                server.server_close()
            if thread is not None:thread.join(5)
