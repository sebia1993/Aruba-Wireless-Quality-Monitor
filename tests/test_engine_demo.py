import unittest,tempfile,time,threading,json
from http.client import HTTPConnection
from pathlib import Path
from http.server import ThreadingHTTPServer
from awqm.fixture_session import workers
from awqm.engine import Engine
from awqm.storage import Store
from awqm.web import handler_for

class EngineDemoTests(unittest.TestCase):
    def test_simulation_and_web_job_api(self):
        t=[1000]
        def clock():return t[0]
        with tempfile.TemporaryDirectory() as root:
            store=Store(Path(root)/"demo")
            e=Engine(workers(clock,clock,False),store,clock=clock,wall=clock,simulation=True,durable_jobs=True,auto=True)
            http=ThreadingHTTPServer(("127.0.0.1",0),handler_for(store,e))
            thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
            try:
                for _ in range(100):
                    e.pulse();time.sleep(.015);t[0]+=1
                    if len(store.snapshot()["radios"])==8:break
                self.assertEqual(len(store.snapshot()["radios"]),8)
                self.assertTrue(all(x["source"]=="synthetic" for x in store.snapshot()["radios"]))
                host=f"127.0.0.1:{http.server_port}"
                def req(method,path,body=None,headers=None):
                    conn=HTTPConnection("127.0.0.1",http.server_port,timeout=3)
                    try:
                        conn.request(method,path,body=body,headers=headers or {})
                        r=conn.getresponse();return r.status,r.read()
                    finally:conn.close()
                status,body=req("GET","/api/snapshot")
                self.assertEqual(status,200)
                snap=json.loads(body);self.assertTrue(snap["engine_demo"])
                data=json.dumps({"controller":"WLC-1","request_id":"test-1","ap_name":"DEMO-AP-001"})
                headers={"Content-Type":"application/json","Origin":"http://"+host,"X-AWQM-Demo":snap["demo_token"]}
                self.assertEqual(req("POST","/api/demo/jobs",data,{"Content-Type":"application/json"})[0],403)
                self.assertEqual(req("POST","/api/demo/jobs",data,{**headers,"Origin":"http://invalid.example"})[0],403)
                invalid=json.dumps({"controller":"WLC-1","request_id":"bad","ap_name":"UNOBSERVED"})
                self.assertEqual(req("POST","/api/demo/jobs",invalid,headers)[0],400)
                status,b=req("POST","/api/demo/jobs",data,headers)
                self.assertEqual(status,200);job=json.loads(b)
                self.assertEqual(json.loads(req("POST","/api/demo/jobs",data,headers)[1])["job_id"],job["job_id"])
                for _ in range(100):
                    e.pulse();time.sleep(.015);t[0]+=1
                    if e.workers["WLC-1"].jobs.get(job["job_id"]).status=="completed":break
                self.assertEqual(e.workers["WLC-1"].jobs.get(job["job_id"]).status,"completed")
                for _ in range(100):
                    e.pulse();time.sleep(.015);t[0]+=1
                    if e.client_views():break
                self.assertGreaterEqual(len(e.client_views()),1)
                self.assertTrue(all(x["source"]=="synthetic" and x["airtime"] is None for x in e.client_views()))
                from urllib.parse import urlencode
                rid=store.snapshot()["radios"][0]["radio_id"]
                query=urlencode(dict(radio_id=rid,start=900,end=t[0]+1,resolution="raw"))
                status,b=req("GET","/api/history?"+query)
                self.assertEqual(status,200)
                self.assertGreater(json.loads(b)["count"],0)
                self.assertEqual(req("GET","/api/history.csv?"+query)[0],200)
                self.assertEqual(req("GET","/api/history.html?"+query)[0],200)
            finally:
                http.shutdown();http.server_close();thread.join();e.close()
