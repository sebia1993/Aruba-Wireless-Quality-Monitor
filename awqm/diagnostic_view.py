"""Bounded, read-only view of field-unvalidated Radio observations.

The writer publishes snapshots; browser refresh does not trigger SSH. Radio
identifiers contain wired MACs internally and are replaced by opaque tokens.
No client MAC, username, SSID or raw CLI data is included in this interface.
"""
import hashlib,hmac,secrets
from threading import RLock
from .reporting import csv_report,html_report

class DiagnosticView:
    def __init__(self,store,clock,key=None):
        if store.mode!="diagnostic":raise ValueError("diagnostic store required")
        self.store=store;self.clock=clock;self.key=key or secrets.token_bytes(32)
        self.lock=RLock();self.rows=[];self.index={};self.meta={}
    def token(self,radio_id):
        return hmac.new(self.key,radio_id.encode("utf-8"),hashlib.sha256).hexdigest()[:32]
    def publish(self,result,current=(),available=True):
        now=int(self.clock())
        with self.lock:
            if available:
                snapshot=self.store.snapshot()
                if snapshot["pending_writes"]:
                    # A multi-file write has not completed. Do not publish a new
                    # coherent view or treat prior observations as current.
                    self.rows=[dict(r,freshness="stale_or_unconfirmed") for r in self.rows]
                    available=False
                current=set(current) if available else set()
                rows=[];index={}
                for r in snapshot["radios"]:
                    if r["source"]!="aos8-cli-unvalidated":continue
                    token=self.token(r["radio_id"])
                    index[token]=r["radio_id"]
                    live=(r["radio_id"],r["observed_at"]) in current
                    rows.append(dict(id=token,ap_name=r["ap_name"],controller=r["controller"],band=r["band"],
                        channel=r["channel"],utilization=r["utilization"],interference=r["interference"],
                        noise=r["noise"],observed_at=r["observed_at"],
                        freshness="observed" if live else "stale_or_unconfirmed",
                        quality="unvalidated"))
                if available:self.rows=rows;self.index=index
            else:
                self.rows=[dict(r,freshness="stale_or_unconfirmed") for r in self.rows]
            self.meta={"published_at":now,"field_validated":False,"mode":"diagnostic",
                "coherent_controllers":result.get("coherent_controllers"),
                "expected_controllers":result.get("expected_controllers"),
                "mapped_radios":result.get("mapped_radios"),
                "active_clients":result.get("active_clients"),
                "unresolved_radios":result.get("unresolved_radios"),
                "client_conflicts":result.get("client_conflicts"),
                "storage_error":result.get("storage_error"),
                "paused":bool(result.get("paused")),
                "available":bool(available)}
    def page(self,query="",page=1,size=100):
        if not isinstance(query,str) or len(query)>128 or not 1<=page<=10000 or not 1<=size<=100:
            raise ValueError("invalid query")
        query=query.casefold()
        with self.lock:
            meta=dict(self.meta);now=int(self.clock())
            stale=not meta.get("available") or not 0<=now-meta.get("published_at",0)<=20
            rows=[r for r in self.rows if any(query in str(r[k]).casefold() for k in ("ap_name","controller","band","channel"))]
            data=[dict(r,freshness="stale_or_unconfirmed" if stale else r["freshness"]) for r in rows[(page-1)*size:page*size]]
            if stale:
                for k in ("coherent_controllers","mapped_radios","active_clients","unresolved_radios","client_conflicts"):meta[k]=None
                meta["available"]=False
            return {**meta,"rows":data,"total":len(rows),"page":page,"size":size,"inventory_radios":len(self.rows)}
    def history(self,token,start,end):
        if not isinstance(token,str) or len(token)!=32 or not 0<=start<end or end-start>7*86400:
            raise ValueError("invalid history request")
        with self.lock:rid=self.index.get(token)
        if rid is None:raise ValueError("unknown Radio")
        if not self.store.lock.acquire(blocking=False):raise RuntimeError("storage busy")
        try:
            if self.store.pending_count():raise RuntimeError("storage pending")
            raw=self.store.history(rid,start,end,"raw",2001)
        finally:self.store.lock.release()
        fields=("ap_name","controller","band","channel","observed_at","utilization","interference","noise","source")
        if any(r.get("source")!="aos8-cli-unvalidated" for r in raw):raise ValueError("invalid diagnostic provenance")
        rows=[{k:r.get(k) for k in fields} for r in raw[:2000]]
        return {"mode":"diagnostic","field_validated":False,"radio_id":token,"start":start,"end":end,"resolution":"raw",
                "count":len(rows),"limit":2000,"truncated":len(raw)>2000,"fields":list(fields),"rows":rows,
                "note":"Field-unvalidated diagnostic observations. No verified quality score, downtime, service impact or root cause."}
