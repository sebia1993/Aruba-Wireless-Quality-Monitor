import tempfile,unittest,json
from pathlib import Path
from awqm.service_config import load_config,envelope,secret,safe_path
from awqm.service_runtime import ConnectionGate,SharedCredentialRejected

class ConfigTests(unittest.TestCase):
    def make(self,root):
        p=Path(root)
        (p/"config").mkdir();(p/"secrets").mkdir();(p/"secrets/known_hosts").touch()
        cfg={"version":1,"mode":"diagnostic","authorized":True,"windows_account":"DOMAIN\\awqm",
             "site":"local-test","known_hosts":"secrets/known_hosts","controllers":[
                {"id":"w1","ip":"192.0.2.1","username":"operator","credential":"secrets/w1.dpapi"},
                {"id":"w2","ip":"192.0.2.2","username":"operator","credential":"secrets/w2.dpapi"}]}
        (p/"config/service.json").write_text(json.dumps(cfg))
        return p,cfg
    def test_binding_and_config(self):
        with tempfile.TemporaryDirectory() as root:
            p,c=self.make(root);cfg=load_config(p)
            t,u=cfg.targets
            t.credential.write_bytes(envelope(t,"fixture",lambda x:b"encoded:"+x))
            self.assertEqual(secret(t,lambda x:x[8:]),"fixture")
            u.credential.write_bytes(t.credential.read_bytes())
            with self.assertRaises(ValueError):secret(u,lambda x:x[8:])
            for bad in ("../x","C:/x","secrets/../x","//host/file","secrets/x:ads"):
                with self.assertRaises(ValueError):safe_path(p,bad)
            c["authorized"]=False;(p/"config/service.json").write_text(json.dumps(c))
            with self.assertRaises(ValueError):load_config(p)
    def test_shared_auth_gate(self):
        with tempfile.TemporaryDirectory() as root:
            p,c=self.make(root);cfg=load_config(p)
            calls=[]
            class AuthenticationException(Exception):pass
            class Fake:
                def __init__(self,host,known):self.host=host
                def connect(self,user,password):calls.append(self.host);raise AuthenticationException()
                def close(self):pass
            g=ConnectionGate(cfg.known_hosts,load_secret=lambda t:"fixture",transport_factory=Fake,shell_factory=lambda x:x)
            for target in cfg.targets:
                with self.assertRaises(SharedCredentialRejected):g.connect(target)
            self.assertEqual(calls,["192.0.2.1"])

    def test_stopping_gate_does_not_connect(self):
        from threading import Event
        from awqm.service_runtime import LoginBusy
        with tempfile.TemporaryDirectory() as root:
            p,c=self.make(root);cfg=load_config(p)
            stop=Event();stop.set()
            def never(*a):raise AssertionError("no connection permitted")
            gate=ConnectionGate(cfg.known_hosts,load_secret=never,transport_factory=never,stop=stop)
            with self.assertRaises(LoginBusy):gate.connect(cfg.targets[0])

    def test_critical_disk_blocks_dispatch(self):
        from collections import namedtuple
        from awqm.service_runtime import pump
        class Stop:
            stopped=False
            def is_set(self):return self.stopped
            def wait(self,interval):self.stopped=True
            def set(self):self.stopped=True
        class Engine:
            closed=False
            def pulse(self):raise AssertionError("critical disk must block dispatch")
            def close(self):self.closed=True
        with tempfile.TemporaryDirectory() as root:
            e=Engine();e.store=type("Store",(),{"root":Path(root)})()
            U=namedtuple("U","total used free")
            status=Path(root)/"status.json"
            pump(e,None,Stop(),status,usage=lambda p:U(100*10**9,96*10**9,4*10**9),wall=lambda:1000)
            self.assertTrue(e.closed)
            self.assertEqual(json.loads(status.read_text())["state"],"stopped")

    def test_explicit_collection_profile(self):
        from awqm.service_config import load_profile
        with tempfile.TemporaryDirectory() as root:
            p,c=self.make(root);cfg=load_config(p)
            self.assertEqual(load_profile(cfg),"safe")
            f=p/"config/collection.json"
            f.write_text(json.dumps({"version":1,"profile":"standard"}))
            with self.assertRaises(ValueError):load_profile(cfg)
            f.write_text(json.dumps({"version":1,"profile":"standard","authorized":True}))
            self.assertEqual(load_profile(cfg),"standard")
