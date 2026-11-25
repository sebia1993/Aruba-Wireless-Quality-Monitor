"""Bound the diagnostic HTTPS request envelope, not application execution.

The normal socket idle timeout is insufficient for a trickling peer.
An independent monotonic timer shuts down an unfinished HTTP connection.
An admitted WebSocket transfers to its existing bounded lifetime.
"""
import http.client,socket,threading

class ConnectionDeadline:
    def __init__(self,connection,seconds):
        if not 0<seconds<=60:raise ValueError("invalid HTTP lifetime")
        self.connection=connection
        self.lock=threading.Lock()
        self.active=True;self.expired=False
        self.timer=threading.Timer(seconds,self._expire)
        self.timer.daemon=True
        self.timer.start()
    def _expire(self):
        with self.lock:
            if not self.active:return
            self.active=False;self.expired=True
            try:self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:pass
    def disarm(self):
        """Synchronize handoff so a cancelled timer cannot close a new WSS."""
        with self.lock:
            self.active=False
            self.timer.cancel()
            return not self.expired

class HeaderBudget:
    """Readline adapter for the stdlib HTTP header parser only."""
    def __init__(self,stream,total=32768,line=8192):
        if not 0<line<=total:raise ValueError("invalid header limits")
        self.stream=stream;self.remaining=total;self.line=line
    def readline(self,size=-1):
        allowed=min(self.remaining,self.line)
        if allowed<=0:raise http.client.HTTPException("header budget exhausted")
        data=self.stream.readline(min(allowed+1,size) if size>=0 else allowed+1)
        if len(data)>allowed:raise http.client.LineTooLong("header budget")
        self.remaining-=len(data)
        return data
