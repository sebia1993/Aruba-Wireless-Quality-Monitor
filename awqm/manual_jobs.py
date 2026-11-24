"""Session-scoped manual AP investigation receipts.

HTTP never executes CLI. Only an opaque, currently published Radio token may
select a target. The existing worker rechecks the attestation, active anchor,
CPU, freshness and budget at execution. Restart never replays a reservation.
"""
import hashlib,json,re,time
from threading import RLock
from uuid import uuid4
from .service_config import safe_path
from .jobs import TERMINAL

COMMAND="show ap debug client-table"
POLICY={"version":1,"enabled":True,"authorized":True}
HEX=re.compile(r"^[0-9a-f]{32}$")
REQUEST=re.compile(r"^[A-Za-z0-9_-]{16,64}$")
class ReservationRejected(ValueError):pass

def bind_ap(wired_mac):
    from .collector import MAC
    if not isinstance(wired_mac,str) or not MAC.fullmatch(wired_mac):raise ReservationRejected("physical_ap_identity_unavailable")
    return hashlib.sha256(("awqm-manual-ap-v1|"+wired_mac.lower()).encode("ascii")).hexdigest()

def load_manual_policy(cfg):
    p=safe_path(cfg.root,"config/manual-detail.json")
    if not p.exists():return False
    if p.stat().st_size>4096:raise ValueError("manual detail policy too large")
    d=json.loads(p.read_text(encoding="utf-8"))
    if d=={"version":1,"enabled":False}:return False
    if d!=POLICY:raise ValueError("explicit manual detail authorization required")
    return True

class ManualReservations:
    def __init__(self,view,workers,audit,stop,clock=time.time,limit=2000):
        self.view=view;self.workers=dict(workers);self.audit=audit
        self.stop=stop;self.clock=clock;self.limit=limit;self.lock=RLock()
        self.epoch=uuid4().hex;self.receipts={};self.requests={}

    def _response(self,receipt):
        w=self.workers[receipt["controller"]]
        now=self.clock()
        with w.jobs.lock:
            j=w.jobs.get(receipt["job_id"])
            pending=w.jobs.pending(now)
            j=w.jobs.get(receipt["job_id"])
            pos=next((i+1 for i,x in enumerate(pending) if x.job_id==receipt["job_id"]),None)
            shared=any(x["job_id"]==receipt["job_id"] and x["controller"]==receipt["controller"]
                and x["receipt_id"]!=receipt["receipt_id"] for x in self.receipts.values())
            owned=bool(j and j.request_id==receipt["internal_request"] and not receipt["joined"] and not shared)
            return {"receipt_id":receipt["receipt_id"],"job_id":receipt["job_id"],
                    "request_id":receipt["request_id"],"controller":receipt["controller"],"ap_name":receipt["ap_name"],
                    "accepted_at":receipt["accepted_at"],"deadline":j.deadline if j else receipt["deadline"],
                    "status":j.status if j else "receipt_unavailable",
                    "reason":j.reason if j else "job_history_evicted",
                    "joined":receipt["joined"],"queue_position":pos,
                    "can_cancel":bool(owned and j.status in ("queued","deferred")),
                    "collection_only":True,"epoch":self.epoch}

    def list(self,user):
        with self.lock:
            mine=[r for r in self.receipts.values() if r["user"]==user]
            return {"enabled":not self.stop.is_set(),"epoch":self.epoch,
                    "rows":[self._response(r) for r in list(reversed(mine))[:100]],
                    "total":len(mine),"restart_replays":False}

    def reserve(self,user,epoch,request_id,token):
        if epoch!=self.epoch:raise ReservationRejected("service_instance_changed")
        if not isinstance(request_id,str) or not REQUEST.fullmatch(request_id) or not isinstance(token,str) or not HEX.fullmatch(token):
            raise ReservationRejected("invalid_request")
        key=(user,request_id)
        with self.lock:
            old=self.requests.get(key)
            if old:
                receipt=self.receipts[old]
                if receipt["token"]!=token:raise ReservationRejected("request_id_conflict")
                return self._response(receipt)
            if self.stop.is_set():raise ReservationRejected("service_stopping")
            if len(self.receipts)>=self.limit:raise ReservationRejected("receipt_capacity_reached")
            target=self.view.manual_target(token)
            w=self.workers.get(target["controller"])
            if w is None or not w.manual_detail or not w.live_detail:raise ReservationRejected("manual_detail_disabled")
            now=self.clock()
            a=w.client_approval;radio=w.field_approval
            if not a or not radio or now>=radio.valid_until or not a.permits(radio.tag,target["observed_at"],now):
                raise ReservationRejected("client_attestation_unavailable")
            if COMMAND in w.detail_disabled:raise ReservationRejected("command_unavailable")
            internal="manual-"+hashlib.sha256((self.epoch+"\0"+user+"\0"+request_id).encode("utf-8")).hexdigest()
            with w.jobs.lock:
                jobs=w.jobs.list(now)
                active=[j for j in jobs if j.name==COMMAND and j.status not in TERMINAL]
                joined=any(j.target==target["ap_name"] for j in active)
                if not joined and len(active)>=5:raise ReservationRejected("controller_ap_queue_full")
                # Bound deliberate repeat investigations, without preventing
                # idempotent retransmission or joining an already active job.
                if not joined and any(j.name==COMMAND and j.target==target["ap_name"] and j.finished is not None
                    and 0<=now-j.finished<60 for j in jobs):
                    raise ReservationRejected("ap_cooldown")
                self.audit.record(user,"manual_job_request",token)
                try:j=w.submit_detail(internal,COMMAND,target["ap_name"],ttl=180,binding=target["binding"])
                except (ValueError,RuntimeError) as exc:raise ReservationRejected("queue_not_accepted") from None
                receipt={"receipt_id":uuid4().hex,"user":user,"token":token,
                    "internal_request":internal,"request_id":request_id,"job_id":j.job_id,
                    "controller":target["controller"],"ap_name":target["ap_name"],
                    "joined":joined or j.request_id!=internal,"accepted_at":now,"deadline":j.deadline}
                self.receipts[receipt["receipt_id"]]=receipt
                self.requests[key]=receipt["receipt_id"]
                return self._response(receipt)

    def cancel(self,user,epoch,receipt_id):
        if epoch!=self.epoch:raise ReservationRejected("service_instance_changed")
        if not isinstance(receipt_id,str) or not HEX.fullmatch(receipt_id):
            raise ReservationRejected("invalid_receipt")
        with self.lock:
            r=self.receipts.get(receipt_id)
            if not r or r["user"]!=user:raise ReservationRejected("unknown_receipt")
            w=self.workers[r["controller"]]
            with w.jobs.lock:
                response=self._response(r)
                if not response["can_cancel"]:return response
                # A later manual join must not be cancelled by another receipt
                # owner. Running and terminal jobs are never interrupted.
                if any(x["job_id"]==r["job_id"] and x["controller"]==r["controller"]
                       and x["receipt_id"]!=receipt_id for x in self.receipts.values()):
                    raise ReservationRejected("shared_job_not_cancellable")
                self.audit.record(user,"manual_job_cancel",receipt_id)
                w.jobs.cancel(r["job_id"],self.clock())
                return self._response(r)
