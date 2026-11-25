import tempfile,unittest,threading
from pathlib import Path
from awqm.auth import Auth,AuthDenied,AuthBusy,ArubaVerifier

class AuthTests(unittest.TestCase):
    def test_roles_sessions_expiry(self):
        t=[0];calls=[]
        def verify(u,p):calls.append(u);return p=="fixture-only"
        auth=Auth({"op":"operator","admin":"admin"},verify,clock=lambda:t[0],idle=30,absolute=100)
        with self.assertRaises(AuthDenied):auth.login("unknown","fixture-only")
        self.assertEqual(calls,[])
        token=auth.login("op","fixture-only")
        s=auth.session(token);self.assertEqual(s.role,"operator")
        self.assertNotIn(token,auth.sessions)
        with self.assertRaises(AuthDenied):auth.session(token,"admin")
        with self.assertRaises(AuthDenied):auth.logout(token,"wrong")
        auth.logout(token,s.csrf)
        with self.assertRaises(AuthDenied):auth.session(token)
        token=auth.login("admin","fixture-only")
        t[0]=31
        with self.assertRaises(AuthDenied):auth.session(token)
        t[0]=61;token=auth.login("op","fixture-only")
        auth.set_roles({"op":"admin"})
        with self.assertRaises(AuthDenied):auth.session(token)
    def test_throttle_and_verifier_failure(self):
        t=[0];calls=[]
        def verify(u,p):calls.append(u);raise RuntimeError("do not leak")
        a=Auth({"op":"operator"},verify,clock=lambda:t[0],user_limit=2,global_limit=5)
        for _ in range(2):
            with self.assertRaises(AuthDenied):a.login("op","not-persisted")
        with self.assertRaises(AuthBusy):a.login("op","not-persisted")
        self.assertEqual(len(calls),2)
        self.assertNotIn("not-persisted",repr(a.audit))
        t[0]=61
        with self.assertRaises(AuthDenied):a.login("op","wrong")
        self.assertEqual(len(calls),3)
    def test_pinned_adapter(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/"known_hosts";p.touch()
            calls=[]
            class Transport:
                def is_authenticated(self):return True
            class SSH:
                def __init__(self,host,path):calls.append(("target",host));self.client=self
                def connect(self,u,p):calls.append(("connect",u))
                def get_transport(self):return Transport()
                def close(self):calls.append(("close",))
            v=ArubaVerifier("192.0.2.10",p,SSH)
            self.assertTrue(v("op","fixture"))
            self.assertEqual(calls,[("target","192.0.2.10"),("connect","op"),("close",)])
