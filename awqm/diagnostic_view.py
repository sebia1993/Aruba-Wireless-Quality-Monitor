"""Bounded, read-only view of field-unvalidated Radio observations.

The writer publishes snapshots; browser refresh does not trigger SSH. Radio
identifiers contain wired MACs internally and are replaced by opaque tokens.
No client MAC, username, SSID or raw CLI data is included in this interface.
"""
import hashlib,hmac,secrets
from copy import deepcopy
from threading import RLock
from .reporting import csv_report,html_report,WINDOWS,timestamp

class DiagnosticView:
    def __init__(self,store,clock,key=None,incidents=None,association_history=None):
        if store.mode!="diagnostic":raise ValueError("diagnostic store required")
        self.store=store;self.clock=clock;self.key=key or secrets.token_bytes(32)
        self.lock=RLock();self.rows=[];self.index={};self.meta={};self.incidents=incidents;self.association_history=association_history;self.client_rows=[];self.client_enabled=False;self.neighbor_views={};self.neighbor_enabled=False
        self.publication_revision=0
    def token(self,radio_id):
        return hmac.new(self.key,radio_id.encode("utf-8"),hashlib.sha256).hexdigest()[:32]
    def publish(self,result,current=(),available=True,quality=None,client_views=(),client_enabled=False,neighbor_views=None,neighbor_enabled=False,anchor_views=None):
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
                    q=(quality or {}).get(r["radio_id"],{})
                    approved=bool(live and q.get("observed_at")==r["observed_at"] and now<q.get("valid_until",0))
                    rows.append(dict(id=token,ap_name=r["ap_name"],controller=r["controller"],band=r["band"],
                        channel=r["channel"],utilization=r["utilization"],interference=r["interference"],
                        noise=r["noise"],observed_at=r["observed_at"],
                        freshness="observed" if live else "stale_or_unconfirmed",
                        quality="local_operator_attested" if approved else "unvalidated",
                        quality_score=q.get("score") if approved else None,
                        risk_status=q.get("status") if approved else "UNKNOWN",
                        approval=q.get("approval") if approved else None,
                        approval_until=q.get("valid_until") if approved else None,
                        incident_id=q.get("incident_id") if approved else None,
                        ap_anchor=deepcopy((anchor_views or {}).get(r["radio_id"])) if live else None))
                if available:self.rows=rows;self.index=index
            else:
                self.rows=[dict(r,freshness="stale_or_unconfirmed") for r in self.rows]
            self.neighbor_enabled=bool(neighbor_enabled)
            self.neighbor_views=dict(neighbor_views or {}) if available else {}
            self.client_enabled=bool(client_enabled)
            self.client_rows=[dict(r) for r in client_views] if available else []
            self.meta={"published_at":now,"field_validated":False,"mode":"diagnostic",
                "coherent_controllers":result.get("coherent_controllers"),
                "expected_controllers":result.get("expected_controllers"),
                "mapped_radios":result.get("mapped_radios"),
                "active_clients":result.get("active_clients"),
                "unresolved_radios":result.get("unresolved_radios"),
                "client_conflicts":result.get("client_conflicts"),
                "storage_error":result.get("storage_error"),"cluster":result.get("cluster") if available else None,
                "paused":bool(result.get("paused")),"operations":result.get("operations"),
                "available":bool(available),"quality_scope":"locally_attested_radio_only"}
            self.publication_revision+=1
    def notification(self):
        with self.lock:
            at=self.meta.get("published_at");now=int(self.clock())
            return {"type":"publication","revision":self.publication_revision,
                    "available":bool(self.meta.get("available") and at is not None and 0<=now-at<=20)}
    def page(self,query="",page=1,size=100,*,token=None):
        if not isinstance(query,str) or len(query)>128 or not 1<=page<=10000 or not 1<=size<=100:
            raise ValueError("invalid query")
        query=query.casefold()
        with self.lock:
            meta=dict(self.meta);meta.pop("operations",None);now=int(self.clock())
            stale=not meta.get("available") or not 0<=now-meta.get("published_at",0)<=20
            if token is None:
                rows=[r for r in self.rows if any(query in str(r[k]).casefold() for k in ("ap_name","controller","band","channel","id"))]
            else:
                rows=[r for r in self.rows if r["id"]==token]
            data=[]
            for r in rows[(page-1)*size:page*size]:
                x=dict(r)
                if stale:x["freshness"]="stale_or_unconfirmed"
                anchor=x.pop("ap_anchor",None)
                if not stale and x["freshness"]=="observed" and anchor:
                    observations=[{k:v for k,v in a.items() if k!="valid_until"} for a in anchor["observations"]
                        if now<a["valid_until"] and 0<=now-a["observed_at"]<=600]
                    x["ap_anchor"]={**anchor,"observations":observations} if observations else None
                else:x["ap_anchor"]=None
                if stale or x["freshness"]!="observed" or (x.get("approval_until") or 0)<=now:
                    x.update(quality="unvalidated",quality_score=None,risk_status="UNKNOWN",approval=None,approval_until=None,incident_id=None)
                data.append(x)
            if stale:
                for k in ("coherent_controllers","mapped_radios","active_clients","unresolved_radios","client_conflicts"):meta[k]=None
                meta["available"]=False;meta["cluster"]=None
            elif meta.get("cluster"):
                # Do not expose expired resource attestation metadata, even
                # between coordinator publications.
                meta["cluster"]=deepcopy(meta["cluster"])
                for node in meta["cluster"].get("nodes",[]):
                    for item in node.get("ap_anchors",{}).values():
                        until=item.pop("valid_until",None)
                        if item.get("status")=="observed" and (until is None or now>=until or not 0<=now-item["observed_at"]<=600):
                            item.update(status="stale",total=None,matched=None,unmatched=None)
                    for label,resource in node.get("resources",{}).items():
                        until=resource.pop("valid_until",None)
                        if resource.get("status")=="observed" and (until is None or now>=until
                            or not 0<=now-resource["observed_at"]<=(360 if label=="memory" else 240)):
                            resource.update(status="stale",data=None)
            return {**meta,"rows":data,"total":len(rows),"page":page,"size":size,"inventory_radios":len(self.rows)}
    def operation_status(self):
        with self.lock:
            now=int(self.clock())
            published=self.meta.get("published_at")
            current=published is not None and 0<=now-published<=20
            return {"published_at":published,"current":current,
                    "state":self.meta.get("operations") if current else None}
    def manual_target(self,token):
        # Deliberately return only an attested current persistent congestion
        # target. Never use a free-form device address, CLI or client MAC.
        from .manual_jobs import ReservationRejected,bind_ap
        with self.lock:
            now=int(self.clock())
            if not self.meta.get("available") or self.meta.get("paused") or not 0<=now-self.meta.get("published_at",0)<=20:
                raise ReservationRejected("observation_unavailable")
            row=next((r for r in self.rows if r["id"]==token),None)
            if (not row or row["freshness"]!="observed" or row["quality"]!="local_operator_attested"
                or row["risk_status"]!="PERSISTENT" or (row.get("approval_until") or 0)<=now
                or not 0<=now-row["observed_at"]<=90 or row["utilization"] is None or row["utilization"]<75):
                raise ReservationRejected("current_attested_congestion_required")
            rid=self.index.get(token,"")
            # Internal IDs are site:wired-MAC:physical-Radio. Only a digest
            # leaves this method; the HTTP receipt omits even that digest.
            try:mac=rid.split(":",1)[1].rsplit(":",1)[0]
            except IndexError:raise ReservationRejected("physical_ap_identity_unavailable") from None
            return {**{k:row[k] for k in ("controller","ap_name","observed_at")},"binding":bind_ap(mac)}
    def association_page(self,ident,page):
        if self.association_history is None:return {"enabled":False,"rows":[],"total":0,"page":page,"size":50}
        return self.association_history.page(int(self.clock()),ident,page)
    def history(self,token,start,end,resolution="raw"):
        if (not isinstance(token,str) or len(token)!=32 or resolution not in WINDOWS
            or type(start) is not int or type(end) is not int or not 0<=start<end or end-start>WINDOWS[resolution]):
            raise ValueError("invalid history request")
        with self.lock:rid=self.index.get(token)
        if rid is None:raise ValueError("unknown Radio")
        # Covers a full ordinary 30-day/365-day series without unbounded
        # export. Multiple configuration epochs per bucket can hit the cap.
        limit=2000 if resolution=="raw" else 9000
        if not self.store.lock.acquire(blocking=False):raise RuntimeError("storage busy")
        try:
            if self.store.pending_count():raise RuntimeError("storage pending")
            samples=self.store.history(rid,start,end,resolution,limit+1)
        finally:self.store.lock.release()
        if any(r.get("source")!="aos8-cli-unvalidated" for r in samples):raise ValueError("invalid diagnostic provenance")
        if resolution=="raw":
            fields=("ap_name","controller","band","channel","observed_at","observed_at_kst","utilization","interference","noise","context_id","source")
            time_field="observed_at"
        else:
            fields=("band","channel","bucket","bucket_kst","observed","valid","risky","utilization_avg","utilization_min","utilization_max","interference_avg","noise_avg","context_id","source")
            time_field="bucket"
        rows=[]
        for sample in samples[:limit]:
            row={k:sample.get(k) for k in fields}
            row[time_field+"_kst"]=timestamp(sample[time_field])
            row["context_id"]=self.token("history-context:"+str(sample.get("epoch","")))[:16]
            rows.append(row)
        return {"mode":"diagnostic","field_validated":False,"radio_id":token,"start":start,"end":end,"resolution":resolution,
                "count":len(rows),"limit":limit,"truncated":len(samples)>limit,"fields":list(fields),"rows":rows,
                "coverage":{"first_at":rows[0][time_field] if rows else None,"last_at":rows[-1][time_field] if rows else None,
                            "observed_samples":len(rows) if resolution=="raw" else sum(r["observed"] for r in rows),
                            "valid_samples":None if resolution=="raw" else sum(r["valid"] for r in rows),
                            "complete_days_only":resolution!="raw"},
                "note":"Field-unvalidated observations. Aggregates cover completed, non-invalidated closed days only; missing intervals are not zero or service recovery. Risk counts are threshold-matching samples, not incident counts, durations or confirmed impact."}
    
    def current_incidents(self):
        with self.lock:
            now=int(self.clock())
            if not self.meta.get("available") or not 0<=now-self.meta.get("published_at",0)<=20:return set()
            return {r["incident_id"] for r in self.rows if r.get("incident_id") and r["freshness"]=="observed"
                    and (r.get("approval_until") or 0)>now and r.get("risk_status")!="UNKNOWN"}

    def incident_list(self,page,status,start,end,query="",size=50):
        if not isinstance(query,str) or len(query)>128 or not 1<=size<=100:
            raise ValueError("invalid incident search")
        if self.incidents is None:
            return {"rows":[],"total":0,"page":page,"size":size,"scope":"no_current_attestation","service_impact":"unverified"}
        return self.incidents.list(page,status,start,end,size=size,current=self.current_incidents(),query=query)

    def incident_detail(self,ident):
        if self.incidents is None:raise ValueError("incident ledger unavailable")
        detail=self.incidents.detail(ident)
        detail["current_observation"]=ident in self.current_incidents() and detail["ended_at"] is None
        detail["display_state"]="UNCONFIRMED" if detail["ended_at"] is None and not detail["current_observation"] else detail["state"]
        return detail

    def incident_review(self,ident,revision,request_id,user,verdict,reason,candidate):
        if self.incidents is None:raise ValueError("incident ledger unavailable")
        return self.incidents.review(ident,revision,request_id,user,verdict,reason,candidate)

    def clients_page(self,query="",page=1,size=100,*,token=None):
        if not isinstance(query,str) or len(query)>128 or type(page) is not int or not 1<=page<=10000 or not 1<=size<=100:
            raise ValueError("invalid client page")
        with self.lock:
            now=int(self.clock())
            fresh=bool(self.meta.get("available") and 0<=now-self.meta.get("published_at",0)<=20)
            fields=("id","ap_name","controller","band","observed_at","health","snr","last_phy_rate","quality_score","quality_status","airtime","retry","candidate","scope","station_retry","station_start","station_end","station_tx_activity","confidence","peer_comparable","peer_degraded","candidate_scope")
            needle = query.casefold()
            matched = [r for r in self.client_rows if self.client_enabled and fresh
                       and 0 <= now-r["observed_at"] <= 180 and now < r.get("valid_until", 0)
                       and (token is None or r["id"] == token)
                       and (not needle or any(needle in str(r[k]).casefold() for k in ("ap_name","id","controller","band")))]
            if token is not None and len(matched) != 1:
                matched = []
            # Apply the separate station attestation expiry at read time as
            # well, even between coordinator publications.
            rows = []
            expires = self.meta.get("published_at", now) + 21
            for row in matched[(page-1)*size:page*size]:
                item = {k:row.get(k) for k in fields}
                expires = min(expires, row["valid_until"], row["observed_at"] + 181)
                if now >= (row.get("candidate_until") or 0):
                    item.update(candidate=None,confidence="unavailable",peer_comparable=None,peer_degraded=None,candidate_scope=None)
                if item.get("station_end") is None or not 0<=now-item["station_end"]<=90 or now >= (row.get("station_until") or 0):
                    item.update(station_retry=None,station_start=None,station_end=None,station_tx_activity="unknown",candidate=None,confidence="unavailable",peer_comparable=None,peer_degraded=None,candidate_scope=None)
                else:
                    expires = min(expires, row["station_until"], item["station_end"] + 91)
                    if item.get("candidate"):
                        expires = min(expires, row["candidate_until"])
                rows.append(item)
            return {"rows":rows,"total":len(matched),"page":page,"size":size,
                "refresh_after":max(0,expires-now) if rows else 0,
                "enabled":self.client_enabled,"available":fresh,"scope":"local_operator_attested_ephemeral_detail",
                "persistent_storage":False,"airtime_validated":False,"cause_confirmed":False}

    def neighbors(self,token):
        if not isinstance(token,str) or len(token)!=32:raise ValueError("invalid Radio")
        with self.lock:
            rid=self.index.get(token)
            if rid is None:raise ValueError("unknown Radio")
            now=int(self.clock());n=self.neighbor_views.get(rid)
            fresh=bool(self.meta.get("available") and 0<=now-self.meta.get("published_at",0)<=20)
            if not fresh or not n or not 0<=now-n["radio_at"]<=90 or not 0<=now-n["observed_at"]<=180 or now>=n["until"]:
                return {"enabled":self.neighbor_enabled,"available":False,"rows":[],"service_impact":"unverified"}
            fields=("neighbor_id","ap_name","band","channel","width","snr","discovery","neighbor_at",
                "target_radio_at","relation","overlap_mhz","candidate","confidence","reason")
            rows=[]
            for item in n["rows"]:
                r={k:item[k] for k in fields}
                if item.get("target_until") is not None and now>=item["target_until"]:
                    r.update(candidate=None,confidence="unavailable",relation="unknown",overlap_mhz=None,reason="target_attestation_expired")
                elif r["candidate"] and (r["neighbor_at"] is None or r["target_radio_at"] is None
                    or not 0<=now-r["neighbor_at"]<=90 or not 0<=now-r["target_radio_at"]<=90):
                    r.update(candidate=None,confidence="unavailable",reason="stale_neighbor_evidence")
                rows.append(r)
            return {"enabled":self.neighbor_enabled,"available":True,"rows":rows,
                    "observed_at":n["observed_at"],"truncated":n["truncated"],"service_impact":"unverified"}
