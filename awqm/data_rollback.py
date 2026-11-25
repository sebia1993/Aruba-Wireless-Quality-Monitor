"""Explicit stopped-service post-start data rollback.

The previous config and secrets must still match the prepared bundle.
Never restore superseded credentials, trust or approvals automatically.
Preserve the current data/log trees and recover interrupted renames from
a verified local journal. No deletion, schema migration, or service startup.
"""
import json,os,shutil
from pathlib import Path
from .update_snapshot import (_path,_walk,_copy,_check_manifest_records,
    _state_inventory,verify)
from .program_switch import locations,_layout,_read_journal,reverse
from .package_stage import digest

DIRS=("data","logs")
FORMAT="awqm-data-rollback-v1"

def _subset(manifest,name):
    prefix=name+"/"
    return {"directories":[x[len(prefix):] for x in manifest["directories"] if x.startswith(prefix)],
        "files":{x[len(prefix):]:v for x,v in manifest["files"].items() if x.startswith(prefix)}}

def _match(root,manifest,protect,check_db=False):
    if not root.exists():return False
    try:return _check_manifest_records(root,manifest,protect,check_db=check_db)
    except (ValueError,OSError,RuntimeError):return False

def _config_unchanged(root,manifest,protect):
    # Compare config/secrets exactly; do not resurrect revoked settings.
    # Ignore only clean empty SQLite sidecars, as in update_snapshot.
    for name in ("config","secrets"):
        base,files,dirs=_walk(root/name,protect)
        observed={}
        for rel,(size,_) in files.items():
            if rel.casefold().endswith(("-wal","-journal","-shm")):
                suffix=next(x for x in ("-wal","-journal","-shm") if rel.casefold().endswith(x))
                if rel[:-len(suffix)] not in files or suffix!="-shm" and size:
                    raise RuntimeError("unclean configuration database")
                continue
            observed[rel]={"size":size,"sha256":digest(base/rel)}
        saved=_subset(manifest["old_state"],name)
        expected={k:{"size":v["size"],"sha256":v["sha256"]} for k,v in saved["files"].items()}
        if observed!=expected or dirs!=saved["directories"]:
            raise RuntimeError("configuration or secrets changed; automatic data rollback prohibited")

def _paths(installed,root,bundle,ident,program_protect,state_protect):
    installed,new,old,journal=locations(installed,ident);root=_path(root);bundle=_path(bundle)
    if root==installed or root in installed.parents or installed in root.parents:
        raise ValueError("separate program and operational roots required")
    # Put the transaction alongside the prepared bundle in the protected
    # backup tree. Never move the directory that contains the bundle.
    backup=_path(root/"backup")
    if backup not in bundle.parents:raise ValueError("bundle must be in protected backup tree")
    tx=_path(bundle.parent/(bundle.name+".data-rollback"))
    if tx==bundle or bundle in tx.parents or tx in bundle.parents:raise ValueError("invalid rollback path")
    if state_protect:
        for p in (root,backup,bundle.parent):state_protect(p)
    if program_protect:program_protect(installed.parent)
    m=verify(bundle,state_protect)
    _read_journal(journal,bundle,m,program_protect)
    return installed,new,old,journal,root,bundle,tx,m

def _capture(root,manifest,include_association,protect):
    # Reject unclean or orphan DB sidecars before capturing the newer trees.
    _state_inventory(root,include_association,protect)
    result={}
    for name in DIRS:
        base,files,dirs=_walk(root/name,protect)
        result[name]={"directories":dirs,"files":{k:{"size":v[0],"sha256":digest(base/k)} for k,v in sorted(files.items())}}
    return result

def _prepare(tx,root,bundle,m,guard,protect,include_association):
    current=_capture(root,m,include_association,protect)
    size=sum(v["size"] for k,v in m["old_state"]["files"].items() if k.split("/",1)[0] in DIRS)
    if shutil.disk_usage(tx.parent).free<size+1024**3:raise RuntimeError("insufficient rollback staging space")
    # Retain an incomplete transaction if preparation fails; never guess
    # that an unfinished copy is a valid restoration source.
    tx.mkdir()
    if protect:protect(tx)
    staged=tx/"staged";saved=tx/"displaced"
    for p in (staged,saved):
        p.mkdir()
        if protect:protect(p)
    for name in DIRS:
        source=bundle/"old-state"/name
        _,files,dirs=_walk(source,protect)
        _copy(source,staged/name,files,dirs,protect,check_db=True)
        _check_manifest_records(staged/name,_subset(m["old_state"],name),protect,check_db=True)
    guard()
    _config_unchanged(root,m,protect)
    for name in DIRS:
        _check_manifest_records(root/name,current[name],protect)
    journal=tx/"journal.json"
    payload={"format":FORMAT,"bundle_sha256":digest(bundle/"update-manifest.json"),
        "include_association_history":include_association,"displaced":current}
    with journal.open("x",encoding="utf-8") as f:
        json.dump(payload,f,sort_keys=True);f.flush();os.fsync(f.fileno())
    if protect:protect(journal)
    return payload

def _load(tx,bundle,protect,include_association):
    if protect:protect(tx)
    if {p.name for p in tx.iterdir()}!={"staged","displaced","journal.json"}:
        raise RuntimeError("incomplete rollback preparation; preserve all paths")
    for name in ("staged","displaced"):
        p=_path(tx/name)
        if not p.is_dir() or not {x.name for x in p.iterdir()}<=set(DIRS):raise ValueError("unexpected rollback tree")
        if protect:protect(p)
    p=_path(tx/"journal.json")
    if protect:protect(p)
    if p.stat().st_size>16*1024*1024:raise ValueError("rollback journal too large")
    j=json.loads(p.read_text(encoding="utf-8"))
    if (not isinstance(j,dict) or set(j)!={"format","bundle_sha256","include_association_history","displaced"}
        or j["format"]!=FORMAT or j["bundle_sha256"]!=digest(bundle/"update-manifest.json")
        or j["include_association_history"] is not include_association or set(j["displaced"])!=set(DIRS)):
        raise ValueError("rollback journal mismatch")
    return j

def _phase(root,tx,name,m,j,protect):
    live=root/name;staged=tx/"staged"/name;saved=tx/"displaced"/name
    previous=_subset(m["old_state"],name);current=j["displaced"][name]
    # Presence, not simply digest equality, determines the rename phase.
    if live.exists() and staged.exists() and not saved.exists():
        if _match(live,current,protect) and _match(staged,previous,protect,True):return "ready"
    elif not live.exists() and staged.exists() and saved.exists():
        if _match(saved,current,protect) and _match(staged,previous,protect,True):return "displaced"
    elif live.exists() and not staged.exists() and saved.exists():
        if _match(saved,current,protect) and _match(live,previous,protect,True):return "restored"
    raise RuntimeError("ambiguous rollback layout; preserve all paths")

def rollback(installed,state_root,bundle,ident,guard,program_protect=None,state_protect=None,
             acknowledge=False,include_association=False,rename=None):
    if acknowledge is not True or type(include_association) is not bool:
        raise ValueError("explicit post-update data displacement authorization required")
    rename=rename or (lambda a,b:a.rename(b))
    guard()
    installed,new,old,journal,root,bundle,tx,m=_paths(installed,state_root,bundle,ident,program_protect,state_protect)
    if m["include_association_history"] and not include_association:
        raise ValueError("explicit association-history authorization required")
    _config_unchanged(root,m,state_protect)
    layout=_layout(installed,new,old,m,program_protect)
    if not tx.exists():
        if layout!="candidate_installed":raise RuntimeError("verified candidate installation required")
        j=_prepare(tx,root,bundle,m,guard,state_protect,include_association)
    else:
        if layout not in ("candidate_installed","switch_interrupted","previous_installed_candidate_staged"):
            raise RuntimeError("unrecognized program switch layout")
        j=_load(tx,bundle,state_protect,include_association)
    # Validate every tree before making any more changes.
    phases={name:_phase(root,tx,name,m,j,state_protect) for name in DIRS}
    if layout!="candidate_installed" and any(p!="restored" for p in phases.values()):
        raise RuntimeError("program reversal preceded data recovery")
    for name in DIRS:
        live=root/name;staged=tx/"staged"/name;saved=tx/"displaced"/name
        guard();_config_unchanged(root,m,state_protect)
        phase=_phase(root,tx,name,m,j,state_protect)
        if phase=="ready":
            rename(live,saved)
            phase=_phase(root,tx,name,m,j,state_protect)
        if phase=="displaced":
            guard()
            rename(staged,live)
        if _phase(root,tx,name,m,j,state_protect)!="restored":
            raise RuntimeError("rollback state not restored")
    guard()
    # The existing reversal validates the complete previous operational
    # state and program manifests. It also handles interrupted reversal.
    result=reverse(installed,root,bundle,ident,guard,program_protect,state_protect,rename=rename)
    for name in DIRS:
        if _phase(root,tx,name,m,j,state_protect)!="restored":raise RuntimeError("rollback verification failed")
    return {"status":"previous_program_and_data_restored_service_not_started",
        "program_status":result["status"],"newer_data_preserved":True,"config_secrets_unchanged":True,
        "service_start_performed":False,"requires_operator_review":True}
