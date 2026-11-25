import json,socket,ssl,tempfile,threading,time,unittest
from pathlib import Path
from types import SimpleNamespace
from ipaddress import IPv4Network
from http.client import HTTPSConnection
from wsproto import WSConnection,ConnectionType
from wsproto.events import Request,AcceptConnection,TextMessage,CloseConnection
from awqm.auth import Auth
from awqm.notifications import Notifications,Busy
from email.message import Message
from awqm.storage import Store
from awqm.diagnostic_view import DiagnosticView
from awqm.diagnostic_web import RestrictedTLS,handler_for
from awqm.secure_preview import tls_context
from awqm.audit import AuditLog
from awqm.web_config import WebConfig,cert_expiry
from test_diagnostic_web import certificate

class NotificationTests(unittest.TestCase):
    def test_handshake_and_slot_limits(self):
        hub=Notifications(None,None,SimpleNamespace(authority="localhost:8443"),limit=2)
        def headers():
            h=Message()
            for k,v in (("Host","localhost:8443"),("Origin","https://localhost:8443"),("Upgrade","websocket"),
                        ("Connection","keep-alive, Upgrade"),("Sec-WebSocket-Version","13"),
                        ("Sec-WebSocket-Key","dGhlIHNhbXBsZSBub25jZQ==")):h[k]=v
            return h
        h=headers()
        ws,reply=hub.handshake(h,"HTTP/1.1","/ws/updates")
        self.assertTrue(reply.startswith(b"HTTP/1.1 101"))
        h=headers();h.replace_header("Origin","https://other.example")
        with self.assertRaises(PermissionError):hub.handshake(h,"HTTP/1.1","/ws/updates")
        h=headers();h["Sec-WebSocket-Key"]="dGhlIHNhbXBsZSBub25jZQ=="
        with self.assertRaises(ValueError):hub.handshake(h,"HTTP/1.1","/ws/updates")
        h=headers();h["Content-Length"]="1"
        with self.assertRaises(ValueError):hub.handshake(h,"HTTP/1.1","/ws/updates")
        with hub.slot("a"):
            with self.assertRaises(Busy):
                with hub.slot("a"):pass
            with hub.slot("b"):
                with self.assertRaises(Busy):
                    with hub.slot("c"):pass
        with hub.slot("c"):pass
        self.assertFalse(hub.active)

    def test_authenticated_publication_and_revocation(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root);cert,key=certificate(p);clock=[1000]
            store=Store(p/"data",mode="diagnostic")
            view=DiagnosticView(store,lambda:clock[0])
            auth=Auth({"op":"operator"},lambda u,p:True,clock=lambda:clock[0],idle=30,absolute=300)
            token=auth.login("op","fixture");session=auth.session(token,touch=False)
            audit=AuditLog(p/"audit.db")
            cfg=WebConfig("127.0.0.1","127.0.0.1:0",0,(IPv4Network("127.0.0.1/32"),),"w1",cert,key,{},cert_expiry(cert,"127.0.0.1"))
            proxy=SimpleNamespace(**vars(cfg));stop=threading.Event()
            server=RestrictedTLS(proxy,handler_for(view,auth,audit,proxy,stop=stop),tls_context(cert,key))
            proxy.authority=f"127.0.0.1:{server.server_port}";origin="https://"+proxy.authority
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            trust=ssl.create_default_context(cafile=str(cert))
            conn=None
            def connect():
                raw=socket.create_connection(("127.0.0.1",server.server_port),timeout=5)
                tls=trust.wrap_socket(raw,server_hostname="127.0.0.1")
                ws=WSConnection(ConnectionType.CLIENT)
                tls.sendall(ws.send(Request(host=proxy.authority,target="/ws/updates",extra_headers=[
                    (b"origin",origin.encode()),(b"cookie",("__Host-awqm_session="+token).encode())])))
                return tls,ws
            def next_event(conn,ws,kind,timeout=5):
                end=time.monotonic()+timeout
                while time.monotonic()<end:
                    for event in ws.events():
                        if isinstance(event,kind):return event
                    data=conn.recv(4096)
                    if not data:break
                    ws.receive_data(data)
                raise AssertionError("expected websocket event missing")
            try:
                # Normal unauthenticated HTTPS must not upgrade.
                c=HTTPSConnection("127.0.0.1",server.server_port,context=trust,timeout=5)
                c.request("GET","/ws/updates");self.assertEqual(c.getresponse().status,401);c.close()
                view.publish({},available=False)
                conn,ws=connect()
                next_event(conn,ws,AcceptConnection)
                msg=json.loads(next_event(conn,ws,TextMessage).data)
                self.assertEqual(msg,{"type":"publication","revision":1,"available":False})
                view.publish({},available=True)
                msg=json.loads(next_event(conn,ws,TextMessage).data)
                self.assertEqual(msg["revision"],2);self.assertTrue(msg["available"])
                self.assertEqual(auth.session(token,touch=False).touched,session.touched)
                auth.logout(token,session.csrf)
                event=next_event(conn,ws,CloseConnection);self.assertEqual(event.code,1008)
                conn.close();conn=None
                token=auth.login("op","fixture")
                conn,ws=connect();next_event(conn,ws,AcceptConnection);next_event(conn,ws,TextMessage)
                conn.sendall(ws.send(TextMessage(data="show version")))
                self.assertEqual(next_event(conn,ws,CloseConnection).code,1008)
            finally:
                if conn is not None:conn.close()
                stop.set();server.shutdown();server.server_close();thread.join(5)
