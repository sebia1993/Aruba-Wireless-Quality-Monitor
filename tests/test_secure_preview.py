import unittest,tempfile,ssl,threading
from pathlib import Path
from datetime import datetime,timedelta,timezone
from http.client import HTTPSConnection
from urllib.parse import urlencode
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from ipaddress import ip_address
from awqm.secure_preview import TLSServer,tls_context,secure_handler
from awqm.auth import Auth
from awqm.storage import Store
from awqm.demo import seed

class PreviewTests(unittest.TestCase):
    def test_tls_login_authorization_logout(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root);key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
            name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,"AWQM test")])
            now=datetime.now(timezone.utc)
            cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5))
                .not_valid_after(now+timedelta(days=1)).add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip_address("127.0.0.1"))]),False)
                .sign(key,hashes.SHA256()))
            (p/"cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            (p/"key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
            ctx=tls_context(p/"cert.pem",p/"key.pem")
            s=Store(p/"data");seed(s,180)
            auth=Auth({"op":"operator","admin":"admin"},lambda u,p:p=="fixture-only")
            server=TLSServer(0,secure_handler(s,auth),ctx)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            trust=ssl.create_default_context(cafile=str(p/"cert.pem"))
            origin=f"https://127.0.0.1:{server.server_port}"
            def req(method,path,body=None,headers=None):
                c=HTTPSConnection("127.0.0.1",server.server_port,context=trust,timeout=5)
                try:
                    c.request(method,path,body=body,headers=headers or {})
                    r=c.getresponse();return r.status,dict(r.getheaders()),r.read()
                finally:c.close()
            try:
                self.assertEqual(req("GET","/api/snapshot")[0],401)
                self.assertEqual(req("GET","/")[0],200)
                body=urlencode({"username":"op","password":"fixture-only"})
                headers={"Content-Type":"application/x-www-form-urlencoded","Origin":origin}
                self.assertEqual(req("POST","/auth/login",body,{"Content-Type":headers["Content-Type"]})[0],403)
                status,h,_=req("POST","/auth/login",body,headers)
                self.assertEqual(status,303)
                cookie=h["Set-Cookie"]
                self.assertIn("Secure",cookie);self.assertIn("HttpOnly",cookie);self.assertIn("SameSite=Strict",cookie)
                token=cookie.split(";",1)[0]
                self.assertEqual(req("GET","/api/snapshot",headers={"Cookie":token})[0],200)
                self.assertEqual(req("GET","/api/admin/status",headers={"Cookie":token})[0],403)
                status,h,_=req("POST","/auth/login",urlencode({"username":"admin","password":"fixture-only"}),headers)
                self.assertEqual(status,303)
                self.assertEqual(req("GET","/api/admin/status",headers={"Cookie":h["Set-Cookie"].split(";",1)[0]})[0],200)
                session=auth.session(token.split("=",1)[1])
                self.assertEqual(req("POST","/auth/logout",urlencode({"csrf":session.csrf}),{**headers,"Cookie":token})[0],303)
                self.assertEqual(req("GET","/api/snapshot",headers={"Cookie":token})[0],401)
            finally:server.shutdown();server.server_close();thread.join()
