import tempfile,unittest,sqlite3
from pathlib import Path
from awqm.auth import Auth,AuthDenied
from awqm.audit import AuditLog

class AuditTests(unittest.TestCase):
    def test_durable_no_credentials(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/"audit.db"
            a=AuditLog(path);auth=Auth({"op":"operator"},lambda u,p:True,audit_sink=a.auth_event)
            token=auth.login("op","fictional-secret")
            auth.logout(token,auth.session(token).csrf)
            b=AuditLog(path)
            self.assertEqual(b.summary()["events"],2)
            c=sqlite3.connect(path)
            try:
                records=c.execute("SELECT username,event,target FROM audit ORDER BY id").fetchall()
                self.assertEqual(records,[("op","login",None),("op","logout",None)])
            finally:c.close()
            self.assertNotIn(token.encode(),path.read_bytes())
            self.assertNotIn(b"fictional-secret",path.read_bytes())
    def test_audit_failure_no_session(self):
        def fail(entry):raise OSError("audit unavailable")
        auth=Auth({"op":"operator"},lambda u,p:True,audit_sink=fail)
        with self.assertRaises(OSError):auth.login("op","fictional")
        self.assertEqual(auth.sessions,{})
