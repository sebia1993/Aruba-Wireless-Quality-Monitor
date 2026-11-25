import io,socket,ssl,tempfile,threading,time,unittest,http.client
from pathlib import Path
from types import SimpleNamespace
from ipaddress import IPv4Network
from http.client import HTTPSConnection
from awqm.http_bounds import HeaderBudget,ConnectionDeadline
from awqm.auth import Auth
from awqm.audit import AuditLog
from awqm.storage import Store
from awqm.diagnostic_view import DiagnosticView
from awqm.diagnostic_web import RestrictedTLS,handler_for
from awqm.secure_preview import tls_context
from awqm.web_config import WebConfig,cert_expiry
from awqm.https_validation import FixtureSocket,certificate
from wsproto.events import AcceptConnection

class HTTPBoundsTests(unittest.TestCase):
    def test_header_budget(self):
        stream=io.BytesIO(b"X: 1\r\nY: 2\r\n\r\n")
        r=HeaderBudget(stream,total=14,line=8)
        self.assertEqual(r.readline(),b"X: 1\r\n")
        self.assertEqual(r.readline(),b"Y: 2\r\n")
        self.assertEqual(r.readline(),b"\r\n")
        with self.assertRaises(http.client.HTTPException):r.readline()
        with self.assertRaises(http.client.LineTooLong):HeaderBudget(io.BytesIO(b"X: "+b"x"*20+b"\r\n"),total=100,line=8).readline()

    def test_disarm_does_not_close_connection(self):
        a,b=socket.socketpair()
        try:
            d=ConnectionDeadline(a,.5);self.assertTrue(d.disarm());d.timer.join(2)
            b.sendall(b"x");a.settimeout(1)
            self.assertEqual(a.recv(1),b"x")
        finally:a.close();b.close()

    def test_actual_tls_request_budget_and_ws_handoff(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root);cert,key=certificate(p)
            store=Store(p/"data",mode="diagnostic");view=DiagnosticView(store,time.time)
            attempts=[];auth=Auth({"op":"operator"},lambda *a:attempts.append(a) or True);token=auth.login("op","fixture")
            audit=AuditLog(p/"audit.db")
            cfg=WebConfig("127.0.0.1","127.0.0.1:0",0,(IPv4Network("127.0.0.1/32"),),"fixture",cert,key,{},cert_expiry(cert,"127.0.0.1"))
            proxy=SimpleNamespace(**vars(cfg));stop=threading.Event()
            server=RestrictedTLS(proxy,handler_for(view,auth,audit,proxy,stop=stop,http_deadline=2),tls_context(cert,key))
            port=server.server_port;proxy.authority=f"127.0.0.1:{port}"
            trust=ssl.create_default_context(cafile=str(cert))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            sock=None;ws=None
            def request(path,headers=None):
                c=HTTPSConnection("127.0.0.1",port,context=trust,timeout=5)
                try:
                    c.request("GET",path,headers=headers or {})
                    r=c.getresponse();r.read();return r.status
                finally:c.close()
            try:
                # Each write arrives before the 10-second idle timeout; the
                # absolute deadline still releases the connection.
                raw=socket.create_connection(("127.0.0.1",port),timeout=5)
                sock=trust.wrap_socket(raw,server_hostname="127.0.0.1")
                sock.sendall(("GET / HTTP/1.1\r\nHost: "+proxy.authority+"\r\nX-Trickle: ").encode())
                began=time.monotonic()
                for _ in range(25):
                    try:sock.sendall(b"a")
                    except OSError:break
                    time.sleep(.12)
                try:
                    sock.settimeout(1);data=sock.recv(1)
                    self.assertEqual(data,b"")
                except (ssl.SSLError,ConnectionError):pass
                self.assertLess(time.monotonic()-began,5)
                sock.close();sock=None
                self.assertEqual(request("/diagnostic.css"),200)
                self.assertEqual(request("/diagnostic.css",{"X-Large":"a"*9000}),431)
                self.assertEqual(request("/diagnostic.css",{"Content-Length":"1"}),400)
                # A partially received authentication body must not start
                # credential verification, even if the header was complete.
                raw=socket.create_connection(("127.0.0.1",port),timeout=5)
                sock=trust.wrap_socket(raw,server_hostname="127.0.0.1")
                sock.sendall(("POST /auth/login HTTP/1.1\r\nHost: "+proxy.authority+"\r\nOrigin: https://"+proxy.authority+
                    "\r\nContent-Type: application/x-www-form-urlencoded\r\nContent-Length: 200\r\n\r\nusername=op").encode())
                sock.settimeout(4)
                try:self.assertEqual(sock.recv(1),b"")
                except (ssl.SSLError,ConnectionError):pass
                sock.close();sock=None
                self.assertEqual(len(attempts),1)
                # Header timer is disarmed only on a successful upgrade.
                view.publish({},[])
                ws=FixtureSocket(proxy.authority,port,trust,token);ws.event((AcceptConnection,));ws.publication(1)
                time.sleep(2.2)
                view.publish({},[]);ws.publication(2)
                self.assertEqual(request("/api/radios?q=&page=1",{"Cookie":"__Host-awqm_session="+token}),200)
            finally:
                if sock:sock.close()
                if ws:ws.close()
                stop.set();server.shutdown();server.server_close();thread.join(5)
