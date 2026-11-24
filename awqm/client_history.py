"""Synthetic detailed-client observation ledger and analysis bridge.

The live diagnostic mode must not persist personally identifying client history
until authorization, retention and privacy controls are implemented. This
ledger accepts only explicit simulation mode. Table and Station Stats counters
are different contracts; do not substitute Tx_Retries/Tx_Pkts for the approved
success-with-retry ratio.
"""
from dataclasses import asdict
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
import sqlite3,json
from .client import Sample,analyze,match_bss
from .model import delta

def map_samples(detail,controller,target,session_id,at,associations,bsses,radios):
    # Use current globally deduplicated Association; no identity from a guessed
    # MAC arithmetic or from a stale/standby AP name alone.
    relevant=[r for r in radios if r.controller==controller and r.ap_name==target]
    if not relevant or len({r.ap_id for r in relevant})!=1:return []
    result=[]
    for d in detail:
        a=associations.get(d["client_mac"])
        if not a or a["ap_name"]!=target or a["bssid"]!=d["bssid"] or a["ssid"]!=d["ssid"]:
            continue
        rid=match_bss(d["bssid"],d["ssid"],bsses,relevant)
        if rid is None:continue
        r=next(x for x in relevant if x.radio_id==rid)
        # Do not treat association or PHY sample as current if RF source is stale.
        if abs(r.observed_at-at)>90:continue
        sample=Sample(d["client_mac"],rid,d["bssid"],d["ssid"],
            session_id+"|"+r.epoch,at,d["tx_packets"],d["rx_packets"],d["health"],
            d["ack_snr"],d["tx_rate"])
        result.append((sample,r,d))
    return result

class ClientHistory:
    def __init__(self,path,simulation=False):
        if not simulation:raise ValueError("synthetic client history only")
        self.path=Path(path);self.lock=RLock()
        with self._db() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS client_samples(
              mac TEXT NOT NULL,at INTEGER NOT NULL,radio_id TEXT NOT NULL,
              sample TEXT NOT NULL,view TEXT NOT NULL,
              PRIMARY KEY(mac,at));
            CREATE INDEX IF NOT EXISTS idx_client_radio ON client_samples(radio_id,at);
            CREATE TABLE IF NOT EXISTS station_samples(
              mac TEXT NOT NULL,at INTEGER NOT NULL,context TEXT NOT NULL,
              sent INTEGER NOT NULL,retried INTEGER NOT NULL,ratio REAL,
              PRIMARY KEY(mac,at));
            """)
    @contextmanager
    def _db(self):
        c=sqlite3.connect(self.path,timeout=5)
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=FULL")
            with c:yield c
        finally:c.close()

    def ingest(self,mapped,persistent_ids):
        """One coherent table result. Store sample and analysis in one transaction."""
        if len(mapped)>2000:raise ValueError("detail table exceeds safe limit")
        with self.lock,self._db() as c:
            contexts=[]
            seen=set()
            for sample,r,raw in mapped:
                if r.source!="synthetic":raise ValueError("non-synthetic client history")
                if sample.client_mac in seen:raise ValueError("ambiguous client sample")
                seen.add(sample.client_mac)
                payload=json.dumps(asdict(sample),sort_keys=True,allow_nan=False)
                old=c.execute("SELECT sample FROM client_samples WHERE mac=? AND at=?",(sample.client_mac,sample.at)).fetchone()
                if old:
                    if old[0]!=payload:raise ValueError("conflicting client observation")
                    continue
                # Never jump over a different association/epoch to invent a delta.
                prev=c.execute("SELECT sample FROM client_samples WHERE mac=? AND at<? ORDER BY at DESC LIMIT 2",(sample.client_mac,sample.at)).fetchall()
                if c.execute("SELECT 1 FROM client_samples WHERE mac=? AND at>? LIMIT 1",(sample.client_mac,sample.at)).fetchone():
                    continue
                before=[Sample(**json.loads(x[0])) for x in prev]
                contexts.append((sample,r,raw,payload,before))
            baseline=[(prior[1],prior[0],s) for s,r,raw,p,prior in contexts if len(prior)==2]
            for s,r,raw,payload,prior in contexts:
                a=analyze(prior[0] if prior else None,s,r,r.radio_id in persistent_ids,baseline,airtime=None)
                row=dict(client_mac=s.client_mac,client_name=s.client_mac,radio_id=s.radio_id,
                    ap_name=r.ap_name,controller=r.controller,ssid=s.ssid,bssid=s.bssid,epoch=s.epoch,band=r.band,health=s.health,
                    snr=s.snr,rate=s.rate,observed_at=s.at,source="synthetic",
                    tx_packets=s.tx,rx_packets=s.rx,radio_health=raw.get("radio_health"),**a)
                if not prior:row["evidence"].append("first_observation")
                row["evidence"].append("airtime_and_aligned_station_retry_unavailable")
                c.execute("INSERT INTO client_samples VALUES(?,?,?,?,?)",
                    (s.client_mac,s.at,s.radio_id,payload,json.dumps(row,allow_nan=False,ensure_ascii=False)))
            return len(contexts)

    def station(self,mac,at,context,counters):
        """Independent Station Stats interval; never inject into a different table window."""
        from .analysis import retry_ratio
        sent,retried=counters["sent"],counters["retried"]
        if not 0<=retried<=sent<=2**63-1:raise ValueError("invalid station counters")
        with self.lock,self._db() as c:
            existing=c.execute("SELECT context,sent,retried FROM station_samples WHERE mac=? AND at=?",(mac,at)).fetchone()
            if existing:
                if existing!=(context,sent,retried):raise ValueError("conflicting station sample")
                return
            old=c.execute("SELECT at,context,sent,retried FROM station_samples WHERE mac=? AND at<? ORDER BY at DESC LIMIT 1",(mac,at)).fetchone()
            ratio=None
            if old and 0<at-old[0]<=180 and old[1]==context:
                ratio=retry_ratio(delta(old[2],sent,old[1],context),delta(old[3],retried,old[1],context))
            c.execute("INSERT INTO station_samples VALUES(?,?,?,?,?,?)",(mac,at,context,sent,retried,ratio))

    def latest(self,now,max_age=180,limit=5000):
        """Latest detailed observation, not an assertion that a client is still connected."""
        with self.lock,self._db() as c:
            rows=c.execute("""SELECT s.view,s.sample FROM client_samples s
                JOIN (SELECT mac,MAX(at) AS at FROM client_samples GROUP BY mac) l
                ON s.mac=l.mac AND s.at=l.at WHERE s.at>=? AND s.at<=?
                ORDER BY s.at DESC,s.mac LIMIT ?""",(now-max_age,now,limit)).fetchall()
            result=[]
            for payload,sample in rows:
                row=json.loads(payload);s=json.loads(sample)
                # Exact association/session context and freshness required, but
                # station ratio remains a separately timestamped statistic.
                prefix=json.dumps([s["epoch"],s["radio_id"],s["bssid"],s["ssid"]],separators=(",",":"))
                st=c.execute("SELECT at,ratio FROM station_samples WHERE mac=? AND context=? AND at>=? AND at<=? ORDER BY at DESC LIMIT 1",
                             (row["client_mac"],prefix,now-max_age,now)).fetchone()
                row["station_retry"]=st[1] if st else None
                row["station_observed_at"]=st[0] if st else None
                result.append(row)
            return result
