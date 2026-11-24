"""Bounded authenticated WSS publication notifications.

Notifications contain a revision and availability only. They never accept
application commands, refresh SSH, or extend an idle login session. Existing
HTTPS endpoints remain the sole source of telemetry.
"""
import base64,binascii,hashlib,json,secrets,threading,time
from contextlib import contextmanager
from wsproto import WSConnection,ConnectionType
from wsproto.events import Request,AcceptConnection,TextMessage,BytesMessage,Ping,Pong,CloseConnection
from wsproto.connection import ConnectionState
from wsproto.utilities import LocalProtocolError,RemoteProtocolError
from .auth import AuthDenied

class Busy(RuntimeError):pass

class Notifications:
    def __init__(self,view,auth,cfg,stop=None,limit=10,clock=time.monotonic):
        if type(limit) is not int or not 1<=limit<=10:raise ValueError("invalid websocket limit")
        self.view=view;self.auth=auth;self.cfg=cfg;self.stop=stop or threading.Event()
        self.limit=limit;self.clock=clock;self.lock=threading.Lock();self.active=set()
    @contextmanager
    def slot(self,token):
        ident=hashlib.sha256(token.encode("ascii")).digest()
        with self.lock:
            if len(self.active)>=self.limit or ident in self.active:raise Busy("notification capacity reached")
            self.active.add(ident)
        try:yield
        finally:
            with self.lock:self.active.discard(ident)
    def handshake(self,headers,version,path):
        if version!="HTTP/1.1" or path!="/ws/updates":raise ValueError("invalid upgrade request")
        if headers.get_all("Host")!=[self.cfg.authority] or headers.get_all("Origin")!=["https://"+self.cfg.authority]:
            raise PermissionError("origin mismatch")
        if headers.get_all("Sec-WebSocket-Version")!=["13"] or len(headers.get_all("Sec-WebSocket-Key",[]))!=1:
            raise ValueError("invalid websocket version/key")
        try:key=base64.b64decode(headers["Sec-WebSocket-Key"],validate=True)
        except (ValueError,binascii.Error):raise ValueError("invalid websocket key") from None
        if len(key)!=16:raise ValueError("invalid websocket key")
        if [x.lower() for x in headers.get_all("Upgrade",[])]!=["websocket"]:raise ValueError("invalid upgrade")
        connection={v.strip().lower() for h in headers.get_all("Connection",[]) for v in h.split(",")}
        if "upgrade" not in connection or headers.get_all("Transfer-Encoding") or headers.get_all("Content-Length",[]) not in ([],["0"]):
            raise ValueError("invalid upgrade framing")
        if headers.get_all("Sec-WebSocket-Protocol"):raise ValueError("subprotocol unsupported")
        ws=WSConnection(ConnectionType.SERVER)
        ws.initiate_upgrade_connection([(k.encode("ascii"),v.encode("latin-1")) for k,v in headers.raw_items()],path)
        events=list(ws.events())
        if len(events)!=1 or not isinstance(events[0],Request):raise ValueError("invalid websocket request")
        # Compression/extension negotiation is intentionally disabled.
        reply=ws.send(AcceptConnection(extra_headers=[(b"cache-control",b"no-store")]))
        return ws,reply

    def run(self,sock,ws,reply,token):
        """After slot admission, write the upgrade and bounded frame loop.

        Only Ping/Pong/Close from the client are valid. wsproto parses frames;
        incoming bytes and control-frame rates are bounded. No application
        message is accumulated. The caller closes the TLS socket on return.
        """
        self.auth.session(token,touch=False)
        if self.stop.is_set() or time.time()>=self.cfg.expires_at:return
        sock.settimeout(1)
        sock.sendall(reply)
        began=self.clock();last=None;ping_at=None;ping_payload=None;next_ping=began+20
        rate_at=began;received=0;events_seen=0
        def close(code):
            if ws.state in (ConnectionState.OPEN,ConnectionState.REMOTE_CLOSING):
                sock.sendall(ws.send(CloseConnection(code=code)))
        try:
            while True:
                now=self.clock()
                if self.stop.is_set() or time.time()>=self.cfg.expires_at or now-began>=300:
                    close(1001);return
                try:self.auth.session(token,touch=False)
                except AuthDenied:close(1008);return
                state=self.view.notification()
                signature=(state["revision"],state["available"])
                if signature!=last:
                    sock.sendall(ws.send(TextMessage(data=json.dumps(state,separators=(",",":")))))
                    last=signature
                if ping_at is not None and now-ping_at>=10:
                    close(1001);return
                if ping_at is None and now>=next_ping:
                    ping_payload=secrets.token_bytes(8);ping_at=now
                    sock.sendall(ws.send(Ping(payload=ping_payload)))
                try:data=sock.recv(4096)
                except TimeoutError:continue
                if not data:return
                if now-rate_at>=10:
                    rate_at=now;received=events_seen=0
                received+=len(data)
                if received>8192:close(1008);return
                ws.receive_data(data)
                for event in ws.events():
                    events_seen+=1
                    if events_seen>40:close(1008);return
                    if isinstance(event,(TextMessage,BytesMessage)):
                        close(1008);return
                    if isinstance(event,CloseConnection):
                        if ws.state==ConnectionState.REMOTE_CLOSING:sock.sendall(ws.send(event.response()))
                        return
                    if isinstance(event,Ping):sock.sendall(ws.send(event.response()))
                    elif isinstance(event,Pong) and ping_at is not None and event.payload==ping_payload:
                        ping_at=None;next_ping=self.clock()+20
        except (OSError,ValueError,LocalProtocolError,RemoteProtocolError):
            # No raw peer data, cookie or exception string is logged.
            return
