import tempfile,unittest,time,threading,ssl,json,csv,io
from pathlib import Path
from datetime import datetime,timedelta,timezone
from ipaddress import IPv4Address,IPv4Network
from http.client import HTTPSConnection
from urllib.parse import urlencode
from types import SimpleNamespace
from cryptography import x509
from cryptography.hazmat.primitives import serialization,hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from awqm.auth import Auth,AuthBusy
from awqm.audit import AuditLog
from awqm.storage import Store
from awqm.model import Radio
from awqm.diagnostic_view import DiagnosticView
from awqm.diagnostic_web import RestrictedTLS,handler_for,CoordinatedVerifier
from awqm.secure_preview import tls_context
from awqm.web_config import WebConfig,cert_expiry,load_web_config
from awqm.scheduler import Protection

def certificate(root,hostname="127.0.0.1"):
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,"AWQM fixture")])
    now=datetime.now(timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5))
          .not_valid_after(now+timedelta(days=1))
          .add_extension(x509.SubjectAlternativeName([x509.IPAddress(IPv4Address(hostname))]),False)
          .sign(key,hashes.SHA256()))
    p=root/"cert.pem";k=root/"key.pem"
    p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    k.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    return p,k

def radio(t):
    return Radio("site:02:00:00:00:00:01:0","site:02:00:00:00:00:01","DEMO-AP","w1","5GHz",44,t,90,5,-94,
                 source="aos8-cli-unvalidated")

class DiagnosticViewTests(unittest.TestCase):
    def test_masking_and_stale(self):
        with tempfile.TemporaryDirectory() as root:
            s=Store(root,mode="diagnostic");r=radio(100);s.save(r);t=[100]
            view=DiagnosticView(s,lambda:t[0],key=b"fixture")
            view.publish({"coherent_controllers":1,"expected_controllers":1},[(r.radio_id,100)])
            page=view.page()
            self.assertEqual(page["rows"][0]["freshness"],"observed")
            self.assertNotIn(r.radio_id,json.dumps(page))
            self.assertEqual(page["rows"][0]["quality"],"unvalidated")
            token=page["rows"][0]["id"]
            report=view.history(token,0,101)
            self.assertNotIn("02:00:00:00:00:01",json.dumps(report))
            self.assertEqual(report["count"],1)
            self.assertEqual(report["mode"],"diagnostic")
            t[0]=121
            self.assertIsNone(view.page()["coherent_controllers"])
            self.assertEqual(view.page()["rows"][0]["freshness"],"stale_or_unconfirmed")
            t[0]=99
            self.assertFalse(view.page()["available"])
    def test_shared_auth_gate(self):
        p=Protection();p.cpu(20,1)
        w=SimpleNamespace(lock=threading.Lock(),schedule=SimpleNamespace(protection=p,paused=False),clock=lambda:1,session=object(),blocked=None)
        gate=SimpleNamespace(lock=threading.Lock())
        stop=threading.Event()
        calls=[]
        v=CoordinatedVerifier(w,gate,lambda u,p:calls.append(u) or True,stop)
        with w.lock:
            with self.assertRaises(AuthBusy):v("op","fixture")
        self.assertEqual(calls,[])
        self.assertTrue(v("op","fixture"))
        p.cpu(85,1)
        with self.assertRaises(AuthBusy):v("op","fixture")
        self.assertEqual(calls,["op"])
        p.cpu(20,2);p.cpu(20,3);p.cpu(20,4)
        w.clock=lambda:4
        p.account(-130,4)
        with self.assertRaises(AuthBusy):v("op","fixture")
        self.assertEqual(calls,["op"])

class WebConfigTests(unittest.TestCase):
    def test_explicit_config(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root);(p/"config").mkdir();(p/"secrets").mkdir()
            c,k=certificate(p/"secrets")
            cfg=SimpleNamespace(root=p,targets=[SimpleNamespace(id="w1",username="collector")])
            f=p/"config/web.json"
            self.assertIsNone(load_web_config(cfg))
            d={"version":1,"enabled":True,"authorized":True,"bind":"127.0.0.1","hostname":"127.0.0.1",
               "port":8443,"allowed_cidrs":["127.0.0.1/32"],"auth_controller":"w1",
               "cert":"secrets/cert.pem","key":"secrets/key.pem","roles":{"op":"operator"}}
            f.write_text(json.dumps(d))
            self.assertEqual(load_web_config(cfg).authority,"127.0.0.1:8443")
            for update in ({"bind":"0.0.0.0"},{"hostname":"wrong.example"},{"allowed_cidrs":["0.0.0.0/0"]},
                           {"roles":{"collector":"admin"}},{"auth_controller":"other"},{"authorized":False}):
                f.write_text(json.dumps({**d,**update}))
                with self.assertRaises(ValueError):load_web_config(cfg)

class DiagnosticHTTPTests(unittest.TestCase):
    def test_authenticated_read_and_export(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root);c,k=certificate(p);t=int(time.time())
            s=Store(p/"data",mode="diagnostic");r=radio(t);s.save(r)
            view=DiagnosticView(s,time.time);view.publish({"coherent_controllers":1},[(r.radio_id,t)])
            audit=AuditLog(p/"audit.db")
            auth=Auth({"op":"operator","admin":"admin"},lambda u,p:p=="fixture",audit_sink=audit.auth_event)
            cfg=WebConfig("127.0.0.1","127.0.0.1:0",0,(IPv4Network("127.0.0.1/32"),),"w1",c,k,{},cert_expiry(c,"127.0.0.1"))
            from dataclasses import replace
            # Handler uses a separate mutable config proxy for the ephemeral test port.
            proxy=SimpleNamespace(**vars(cfg))
            server=RestrictedTLS(proxy,handler_for(view,auth,audit,proxy),tls_context(c,k))
            proxy.authority=f"127.0.0.1:{server.server_port}"
            self.assertFalse(server.verify_request(None,("192.0.2.10",1234)))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            trust=ssl.create_default_context(cafile=str(c));origin="https://"+proxy.authority
            def req(method,path,body=None,headers=None):
                con=HTTPSConnection("127.0.0.1",server.server_port,context=trust,timeout=5)
                try:
                    con.request(method,path,body=body,headers=headers or {})
                    resp=con.getresponse();return resp.status,dict(resp.getheaders()),resp.read()
                finally:con.close()
            try:
                self.assertEqual(req("GET","/api/radios?q=&page=1")[0],401)
                self.assertEqual(req("GET","/",headers={"Host":"invalid.example"})[0],403)
                body=urlencode({"username":"op","password":"fixture"})
                form={"Content-Type":"application/x-www-form-urlencoded","Origin":origin}
                self.assertEqual(req("POST","/auth/login",body,{"Content-Type":form["Content-Type"]})[0],403)
                status,headers,_=req("POST","/auth/login",body,form)
                self.assertEqual(status,303)
                cookie=headers["Set-Cookie"].split(";",1)[0]
                self.assertIn("Secure",headers["Set-Cookie"])
                h={"Cookie":cookie}
                status,_,data=req("GET","/api/radios?q=&page=1",headers=h)
                self.assertEqual(status,200)
                self.assertNotIn(r.radio_id,data.decode())
                row=json.loads(data)["rows"][0]
                q=urlencode({"id":row["id"],"start":t-10,"end":t+1})
                status,_,data=req("GET","/api/history.csv?"+q,headers=h)
                self.assertEqual(status,200)
                self.assertIn("diagnostic",data.decode("utf-8-sig"))
                self.assertNotIn("02:00:00:00:00:01",data.decode("utf-8-sig"))
                self.assertEqual(req("GET","/api/admin/status",headers=h)[0],403)
                self.assertEqual(req("GET","/api/history.html?"+q,headers=h)[0],200)
                self.assertEqual(req("POST","/api/demo/jobs","",form)[0],404)
                token=cookie.split("=",1)[1]
                csrf=auth.session(token).csrf
                self.assertEqual(req("POST","/auth/logout",urlencode({"csrf":csrf}),{**form,**h})[0],303)
                self.assertEqual(req("GET","/api/radios?q=&page=1",headers=h)[0],401)
                self.assertGreaterEqual(audit.summary()["events"],4)
            finally:server.shutdown();server.server_close();thread.join()
