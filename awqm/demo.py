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
        store.save_batch(normalized_snapshot(fixture))


def synthetic_clients(radios):
    """Generate explicit synthetic evidence using the same analysis functions.

    Only current synthetic radios are used; never feed real device data here.
    """
    from .client import Sample,Airtime,analyze
    from .model import Radio
    out=[]
    for row in radios:
        if row.get("source")!="synthetic":
            continue
        radio=Radio(**{k:row[k] for k in Radio.__dataclass_fields__})
        t=radio.observed_at
        busy=bool(row.get("open_incident")) and radio.utilization>=75
        # Three comparable windows; all data are deliberately invented.
        def sample(i,at,packets,health,retries,rate,snr):
            client=f"02:00:00:{int(radio.ap_name.rsplit('-',1)[1]):02x}:{int(radio.radio_id.rsplit(':',1)[1]):02x}:{i:02x}"
            # The active comparison cohort shares a network. The fourth client demonstrates the other SSID.
            network=1 if i<4 else 2
            bssid=f"02:ff:00:{int(radio.ap_name.rsplit('-',1)[1]):02x}:{int(radio.radio_id.rsplit(':',1)[1]):02x}:{network:02x}"
            return Sample(client,radio.radio_id,bssid,"DEMO-GUEST" if network==1 else "DEMO-STAFF",
                          radio.epoch,at,packets,packets,health,snr,rate,sent=packets,retried=retries)
        history=[]
        for i in range(1,5):
            low=busy and i in (1,2,3)
            idle=i==4
            before=sample(i,t-120,1000,90,5,600,35)
            previous=sample(i,t-60,1200,90,10,600,35)
            current=sample(i,t,1200 if idle else 1500,
                           22 if i==1 and low else 45 if low else 20 if idle else 92,
                           10 if idle else 110 if low else 15,
                           24 if i==1 and low else 12 if idle else 600,
                           12 if i==1 and low else 10 if idle else 34)
            history.append((before,previous,current))
        for i,(old,p,c) in enumerate(history,1):
            air=Airtime(43 if busy and i==1 else 4,"measured","whole_interval",p.at,c.at,c.epoch,True) if i!=4 else None
            result=analyze(p,c,radio,busy,history,air)
            out.append(dict(client_mac=c.client_mac,client_name=f"DEMO-{radio.ap_name[8:]}-{radio.radio_id[-1]}-{i}",
                            radio_id=radio.radio_id,ap_name=radio.ap_name,ssid=c.ssid,band=radio.band,
                            health=c.health,snr=c.snr,rate=c.rate,observed_at=c.at,source="synthetic",**result))
    return out
