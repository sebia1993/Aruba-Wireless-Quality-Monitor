"""Explicit local, read-only collection trial. Not an always-on service.

The operator alone supplies authorized device addresses, trusted known_hosts,
and credentials in a local console. This mode never starts a web listener.
No credentials or raw CLI responses are stored or printed.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from getpass import getpass,GetPassWarning
import ipaddress
import json
from pathlib import Path
import shutil
import sys
import time
import warnings

from .collector import PinnedSSH
from .ssh_shell import ShellSession
from .aos8 import ControllerSnapshot,ParseError,radio_summary,inventory,active_details,associations,cpu_load,merge_radios,merge_associations
from .client import bss_table
from .worker import DENIED,UNSUPPORTED
from .storage import Store,disk_level

# A short, fixed allowlist. One attempt per command and no debug commands.
COMMANDS=(
    ("show cpuload",cpu_load,10),
    ("show ap radio-summary",radio_summary,30),
    ("show ap active details",active_details,60),
    ("show ap database long",inventory,60),
    ("show ap association",associations,60),
    ("show ap bss-table",bss_table,60),
)
# Use a separate limit from the continuous scheduler. No retry/backfill.
AUTH_FAILURES={"AuthenticationException","BadAuthenticationType","PasswordRequiredException"}

@dataclass
class Result:
    controller:str
    commands:list
    snapshot:ControllerSnapshot | None
    bsses:list | None=None

def collect_readings(session,clock=time.monotonic,wall=time.time,budget=30,*,
                     commands=COMMANDS,continue_on_denied=False,stopped=None):
    """One CPU-gated, bounded round. Each successful command is independent evidence."""
    if not commands or commands[0] != COMMANDS[0] or any(cmd not in COMMANDS for cmd in commands):
        raise ValueError("CPU must precede supported read-only commands")
    start=clock()
    results={};at={};out=[]
    for name,parse,maximum in commands:
        if stopped is not None and stopped.is_set():
            break
        remaining=budget-(clock()-start)
        if remaining<1:
            out.append({"command":name,"status":"budget_deferred"})
            continue
        try:
            raw=session.run(name,timeout=min(maximum,remaining),idle_timeout=min(10,remaining))
        except Exception as exc:
            out.append({"command":name,"status":"transport_error","error":type(exc).__name__})
            break
        try:
            if DENIED.search(raw):
                out.append({"command":name,"status":"permission_denied"})
                if name=="show cpuload" or not continue_on_denied:break
                continue
            if UNSUPPORTED.search(raw):
                out.append({"command":name,"status":"unsupported"})
                if name=="show cpuload":break
                continue
            value=parse(raw)
            results[name]=value;at[name]=int(wall())
            out.append({"command":name,"status":"parsed","rows":len(value) if isinstance(value,list) else None})
        except (ValueError,TypeError,ParseError):
            out.append({"command":name,"status":"parse_error"})
            if name=="show cpuload":break
            continue
        if name=="show cpuload" and (value is None or value>=70):
            out.append({"command":"remaining","status":"cpu_protection"})
            break
    return results,at,out

def collect_controller(controller,session,clock=time.monotonic,wall=time.time,budget=30):
    results,at,out=collect_readings(session,clock,wall,budget)
    required=("show ap database long","show ap active details","show ap radio-summary")
    if any(k not in results for k in required) or max(at[k] for k in required)-min(at[k] for k in required)>45:
        return Result(controller,out,None)
    assoc=results.get("show ap association")
    return Result(controller,out,ControllerSnapshot(controller,at[required[2]],results[required[0]],
        results[required[1]],results[required[2]],assoc,inventory_at=at[required[0]],anchor_at=at[required[1]],
        association_at=at.get("show ap association")),results.get("show ap bss-table"))

def run_trial(targets,session_factory,store,clock=time.monotonic,wall=time.time):
    """Sequential initial logins, parallel independent bounded command rounds.

    A rejected shared login stops new attempts to avoid account lockouts.
    Missing/failed controllers are reported; their APs are never marked Down.
    """
    if not 1<=len(targets)<=4 or len({name for name,_ in targets})!=len(targets) or len({host for _,host in targets})!=len(targets):
        raise ValueError("one to four distinct controllers required")
    sessions=[];connection={};blocked=False
    try:
        for name,host in targets:
            if blocked:
                connection[name]="authentication_attempts_stopped"
                continue
            session=None
            try:
                session=session_factory(host)
                session.open()
                sessions.append((name,session))
                connection[name]="connected"
            except Exception as exc:
                connection[name]=type(exc).__name__
                if session is not None:
                    try:session.close()
                    except Exception:pass
                if type(exc).__name__ in AUTH_FAILURES:
                    blocked=True
        # An authentication rejection is a stop condition for this trial.
        if blocked:
            return {"mode":"diagnostic","field_validated":False,"status":"authentication_rejected",
                    "connections":connection,"coherent_controllers":0,"expected_controllers":len(targets),
                    "merged_radios":0,"merged_clients":0,"written":0}
        with ThreadPoolExecutor(max_workers=max(1,len(sessions))) as pool:
            jobs=[pool.submit(collect_controller,n,s,clock,wall) for n,s in sessions]
            results=[f.result() for f in jobs]
        snapshots=[r.snapshot for r in results if r.snapshot is not None]
        radios,unresolved=merge_radios(snapshots,site="local-trial")
        clients,conflicts=merge_associations(snapshots)
        written=0
        for i in range(0,len(radios),1000):
            written+=store.save_batch(radios[i:i+1000])
        return {"mode":"diagnostic","field_validated":False,
            "status":"observations_available" if radios else "no_coherent_radio_data",
            "connections":connection,"coherent_controllers":len(snapshots),
            "expected_controllers":len(targets),"merged_radios":len(radios),"merged_clients":len(clients),
            "unresolved_radios":len(unresolved),"client_conflicts":len(conflicts),"written":written,
            "controllers":[{"controller":r.controller,"commands":r.commands,
                            "coherent":r.snapshot is not None,"bss_rows":None if r.bsses is None else len(r.bsses)} for r in results],
            "quality_assessment":"not_field_validated","continuous_collection":False}
    finally:
        for _,session in sessions:
            try:session.close()
            except Exception:pass

def main():
    p=argparse.ArgumentParser(description="AWQM local read-only diagnostic trial; no web access")
    p.add_argument("--host",action="append",required=True,help="approved controller management IP, repeat up to four times")
    p.add_argument("--known-hosts",type=Path,required=True,help="existing trusted OpenSSH known_hosts; unknown keys rejected")
    p.add_argument("--data-dir",type=Path,required=True,help="approved protected local directory for diagnostic DB")
    p.add_argument("--authorized",action="store_true",help="confirm operator approval for this limited read-only trial")
    a=p.parse_args()
    if not a.authorized:p.error("operator authorization must be explicit")
    try:hosts=[str(ipaddress.ip_address(x)) for x in a.host]
    except ValueError:p.error("management IP literals only")
    if not 1<=len(hosts)<=4 or len(set(hosts))!=len(hosts):p.error("one to four unique hosts required")
    if not a.known_hosts.is_file():p.error("trusted known_hosts file required")
    if not sys.stdin.isatty():p.error("interactive console required")
    # No remote shares; the operator must protect the selected local directory.
    if str(a.data_dir.resolve()).startswith(("\\\\","//")):p.error("local directory required")
    try:
        a.data_dir.mkdir(parents=True,exist_ok=True)
        usage=shutil.disk_usage(a.data_dir)
        if disk_level(usage.free,usage.total)=="critical":p.error("insufficient local disk capacity")
        store=Store(a.data_dir,mode="diagnostic")
    except (OSError,ValueError,RuntimeError):
        p.error("protected diagnostic data directory unavailable")
    print("Local diagnostic only; no web listener or continuous collection.",flush=True)
    user=input("Approved controller username: ").strip()
    if not user:p.error("username required")
    with warnings.catch_warnings():
        warnings.simplefilter("error",GetPassWarning)
        try:password=getpass("Controller password (not stored): ")
        except GetPassWarning:p.error("secure password input unavailable")
    if not password:p.error("password required")
    def factory(host):
        ssh=PinnedSSH(host,str(a.known_hosts.resolve()))
        ssh.connect(user,password)
        return ShellSession(ssh)
    try:
        result=run_trial([(f"WLC-{i+1}",host) for i,host in enumerate(hosts)],factory,store)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        if result["status"]=="authentication_rejected" or not result["merged_radios"]:
            raise SystemExit(2)
    finally:
        del password

if __name__=="__main__":main()
