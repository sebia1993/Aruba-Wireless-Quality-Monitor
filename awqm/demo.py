"""Deterministic, synthetic-only data. No network credentials or site identifiers."""
from dataclasses import asdict
import json, time
from .model import Radio
from .parser import normalized_snapshot

def synthetic_rows(now=None, count=12):
    now=int(time.time()) if now is None else now
    for i in range(count):
        ap=f"DEMO-AP-{i//2+1:03d}"
        band="5GHz" if i%2==0 else "2.4GHz"
        busy=i in (0,2,5)
        yield Radio(f"{ap}:{i%2}",ap,ap,f"WLC-{(i//2)%4+1}",band,44 if i%2==0 else 6,
                    now,90 if busy else 20+i,42 if i==5 else 5,-78 if i==5 else -94,
                    20+i,epoch="demo-1")
def seed(store, now=None):
    now=int(time.time()) if now is None else now
    for offset in (120,60,0):
        rows=[asdict(r) for r in synthetic_rows(now-offset)]
        fixture=json.dumps({"schema":"awqm.synthetic.radio.v1","complete":True,"count":len(rows),"rows":rows})
        for row in normalized_snapshot(fixture):
            store.save(row)
