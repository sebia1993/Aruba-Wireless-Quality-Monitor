"""Local fixture-engine dashboard. Real diagnostic storage is never opened."""
import json,tempfile,threading,time
from pathlib import Path
from http.server import ThreadingHTTPServer
from .engine import Engine
from .fixture_session import workers
from .storage import Store
from .web import handler_for

def serve_engine(port=8765,once=False):
    with tempfile.TemporaryDirectory(prefix="awqm-engine-demo-") as root:
        store=Store(Path(root)/"demo")
        if once:
            # Run the real worker/merge/store/queue path using a controlled clock.
            current=[int(time.time())]
            def clock():return current[0]
            engine=Engine(workers(clock,clock,False),store,clock=clock,wall=clock,simulation=True,durable_jobs=True,auto=True)
            try:
                for _ in range(100):
                    engine.pulse()
                    time.sleep(.015)
                    current[0]+=1
                    if len(store.snapshot()["radios"])==8:break
                if len(store.snapshot()["radios"])!=8:
                    raise RuntimeError("fixture engine smoke coverage failed")
                job=engine.submit("WLC-1","smoke-request","show ap debug client-table","DEMO-AP-001")
                for _ in range(100):
                    engine.pulse()
                    time.sleep(.015)
                    current[0]+=1
                    if engine.workers["WLC-1"].jobs.get(job.job_id).status=="completed":break
                if engine.workers["WLC-1"].jobs.get(job.job_id).status!="completed":
                    raise RuntimeError("fixture job smoke failed")
                print(json.dumps({"mode":"engine-demo","radios":8,"job":"completed","field_validated":False}))
            finally:engine.close()
            return
        engine=Engine(workers(time.monotonic,time.time),store,simulation=True,durable_jobs=True,auto=True)
        stop=threading.Event()
        def pump():
            try:engine.run(stop)
            finally:stop.set()
        thread=threading.Thread(target=pump,daemon=True)
        server=ThreadingHTTPServer(("127.0.0.1",port),handler_for(store,engine))
        thread.start()
        print(f"AWQM synthetic engine demo: http://127.0.0.1:{port}",flush=True)
        try:server.serve_forever()
        finally:
            stop.set();thread.join();server.server_close()
