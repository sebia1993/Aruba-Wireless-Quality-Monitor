"""Actual local SSH handshake and shell regression for audited dependencies.

All endpoints are ephemeral IPv4 loopback sockets. Keys are freshly generated,
the username/password and response are invented, and the only supported
command is an allowlisted fixture CPU query. No real WLC, external service,
credential input, or filesystem trust outside a fresh temporary directory.
"""
import json,socket,tempfile,threading,time
from pathlib import Path
from contextlib import contextmanager
import paramiko
from cryptography import __version__ as crypto_version
from .collector import PinnedSSH,HostKeyUnapproved
from .ssh_shell import ShellSession
from .field_validation import fingerprint
from .auth import ArubaVerifier

USER="fixture-operator"
PASSWORD="fixture-only"
PROMPT="(AWQM-FIXTURE) # "
CPU="user 10%, system 10%, idle 80%"

class FixtureInterface(paramiko.ServerInterface):
    def __init__(self):
        self.attempts=[];self.shell_requests=0;self.commands=[]
        self.shell_ready=threading.Event()
    def get_allowed_auths(self,username):return "password"
    def check_auth_password(self,username,password):
        # Do not retain even the fixture password in the test evidence.
        self.attempts.append(username)
        return paramiko.AUTH_SUCCESSFUL if username==USER and password==PASSWORD else paramiko.AUTH_FAILED
    def check_channel_request(self,kind,chanid):
        return paramiko.OPEN_SUCCEEDED if kind=="session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
    def check_channel_pty_request(self,channel,term,width,height,pixelwidth,pixelheight,modes):
        return True
    def check_channel_shell_request(self,channel):
        self.shell_requests+=1;self.shell_ready.set();return True

@contextmanager
def server(key):
    listener=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    listener.bind(("127.0.0.1",0));listener.listen(1);listener.settimeout(5)
    interface=FixtureInterface();stop=threading.Event();state={}
    port=listener.getsockname()[1]
    def run():
        conn=None;transport=None;channel=None
        try:
            conn,_=listener.accept();conn.settimeout(5)
            transport=paramiko.Transport(conn);state["transport"]=transport
            transport.add_server_key(key)
            transport.start_server(server=interface)
            # An auth-only client never opens a shell. Observe until it closes.
            while not stop.is_set() and transport.is_active():
                if channel is None:
                    channel=transport.accept(.1)
                    if channel is None:continue
                if not interface.shell_ready.wait(.1):continue
                channel.settimeout(5)
                channel.sendall(PROMPT.encode("ascii"))
                received=b""
                while not received.endswith(b"\n") and len(received)<1024:
                    chunk=channel.recv(1024)
                    if not chunk:break
                    received+=chunk
                if received!=b"show cpuload\n":raise RuntimeError("unexpected fixture command")
                interface.commands.append("show cpuload")
                channel.sendall(("show cpuload\r\n"+CPU+"\r\n"+PROMPT).encode("ascii"))
                stop.wait(5)
                break
        except Exception as exc:
            state["error"]=type(exc).__name__
        finally:
            if channel is not None:channel.close()
            if transport is not None:transport.close()
            if conn is not None:conn.close()
    thread=threading.Thread(target=run,name="awqm-ssh-fixture",daemon=True);thread.start()
    try:
        yield port,interface,state
    finally:
        stop.set();listener.close()
        # A rejected or closed client normally terminates the server itself.
        # Force-close remaining fixture transport on teardown.
        t=state.get("transport")
        if t is not None:t.close()
        thread.join(7)
        if thread.is_alive():raise RuntimeError("SSH fixture did not stop")

def known_file(root,port,key):
    path=root/"known_hosts"
    path.write_text("[127.0.0.1]:"+str(port)+" "+key.get_name()+" "+key.get_base64()+"\n",encoding="ascii")
    return path

def validate():
    started=time.perf_counter()
    # Generate only test keys; no external key files or private material
    # written to disk. Use RSA key material with SHA-2 SSH signatures.
    trusted=paramiko.RSAKey.generate(2048)
    changed=paramiko.RSAKey.generate(2048)
    outcomes={}
    with tempfile.TemporaryDirectory(prefix="awqm-ssh-fixture-") as tmp:
        root=Path(tmp)
        with server(trusted) as (port,interface,state):
            ssh=PinnedSSH("127.0.0.1",str(known_file(root,port,trusted)),port=port)
            try:
                ssh.connect(USER,PASSWORD)
                transport=ssh.client.get_transport()
                if transport.host_key_type not in ("rsa-sha2-256","rsa-sha2-512"):
                    raise RuntimeError("unexpected negotiated RSA signature algorithm")
                session=ShellSession(ssh)
                try:
                    if not (fingerprint(session) or "").startswith("SHA256:"):
                        raise RuntimeError("missing pinned server fingerprint")
                    session.open()
                    if session.run("show cpuload",timeout=5,idle_timeout=5)!=CPU:
                        raise RuntimeError("fixture command result mismatch")
                    if interface.attempts!=[USER] or interface.commands!=["show cpuload"]:
                        raise RuntimeError("unexpected auth or command attempts")
                    outcomes["pinned_rsa_sha2_shell"]="pass"
                finally:session.close()
            finally:ssh.close()
        with server(trusted) as (port,interface,state):
            path=known_file(root,port,trusted)
            # Reuse actual auth-only verification path with fixture endpoint.
            verifier=ArubaVerifier("127.0.0.1",path,lambda host,known:PinnedSSH(host,known,port))
            if not verifier(USER,PASSWORD) or interface.attempts!=[USER] or interface.shell_requests:
                raise RuntimeError("auth-only path failed")
            outcomes["auth_only_no_shell"]="pass"
        with server(trusted) as (port,interface,state):
            ssh=PinnedSSH("127.0.0.1",str(known_file(root,port,changed)),port=port)
            try:
                try:ssh.connect(USER,PASSWORD)
                except paramiko.BadHostKeyException:pass
                else:raise RuntimeError("changed host key accepted")
                if ssh.client is not None or interface.attempts:
                    raise RuntimeError("changed host key reached authentication")
                outcomes["changed_key_rejected_before_auth"]="pass"
            finally:ssh.close()
        with server(trusted) as (port,interface,state):
            ssh=PinnedSSH("127.0.0.1",str(known_file(root,port,trusted)),port=port)
            try:
                try:ssh.connect(USER,"wrong-fixture-password")
                except paramiko.AuthenticationException:pass
                else:raise RuntimeError("bad password accepted")
                if ssh.client is not None or interface.attempts!=[USER] or interface.shell_requests:
                    raise RuntimeError("unexpected rejected-login behavior")
                outcomes["bad_password_rejected"]="pass"
            finally:ssh.close()
        path=root/"empty-known-hosts";path.touch()
        # No listener: the missing pin must fail BEFORE any TCP attempt.
        ssh=PinnedSSH("127.0.0.1",str(path),port=1)
        try:
            try:ssh.connect(USER,PASSWORD)
            except HostKeyUnapproved:pass
            else:raise RuntimeError("missing host key accepted")
            if ssh.client is not None:raise RuntimeError("client retained after missing pin")
            outcomes["missing_pin_rejected_before_connect"]="pass"
        finally:ssh.close()
    return {"result":"pass","scope":"actual loopback SSH and synthetic auth only",
        "paramiko":paramiko.__version__,"cryptography":crypto_version,
        "checks":outcomes,"actual_elapsed_seconds":round(time.perf_counter()-started,6),
        "real_wlc_validated":False,"enterprise_aaa_validated":False,"scm_validated":False}

def main():
    print(json.dumps(validate(),indent=2,ensure_ascii=False))
