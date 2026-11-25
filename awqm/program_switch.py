"""Guarded offline program-only switch and pre-start reversal.

The caller must hold the exclusive service guard and prevent SCM startup.
No state migration, service start, deletion of rollback files or overwrite.
A small journal and exact manifest checks permit recovery after either rename.
The existing verified snapshot is required; the candidate ZIP is re-verified
against an independently trusted digest at switch time.
"""
import json,os,re
from pathlib import Path
from .update_snapshot import (_path,_walk,_state_inventory,_check_manifest_records,
    _validate_program,_safe_part,verify)
from .package_stage import stage,digest

ID=re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
FORMAT="awqm-program-switch-v1"

def locations(installed,ident):
    p=_path(installed)
    if not isinstance(ident,str) or not ID.fullmatch(ident) or not _safe_part(ident):
        raise ValueError("invalid switch ID")
    stem=p.name+".switch-"+ident
    return p,p.parent/(stem+".new"),p.parent/(stem+".old"),p.parent/(stem+".json")

def state_matches(root,manifest,protect=None):
    root=_path(root)
    files,dirs=_state_inventory(root,manifest["include_association_history"],protect)
    saved=manifest["old_state"]
    if set(files)!=set(saved["files"]) or dirs!=saved["directories"]:
        raise RuntimeError("operational state changed since snapshot")
    for rel,(size,_) in files.items():
        record=saved["files"][rel]
        if size!=record["size"] or digest(root/rel)!=record["sha256"]:
            raise RuntimeError("operational state changed since snapshot")

def _journal(path,bundle,manifest,guard,protect):
    guard()
    if path.exists():raise ValueError("switch journal already exists; inspect/reverse it")
    payload={"format":FORMAT,"bundle_manifest_sha256":digest(bundle/"update-manifest.json"),
             "archive_sha256":manifest["archive_sha256"],"old_version":manifest["old_version"],
             "new_version":manifest["new_version"]}
    # No overwrite. A partially written journal fails closed; leave copies.
    with path.open("x",encoding="utf-8") as f:
        json.dump(payload,f,sort_keys=True);f.flush();os.fsync(f.fileno())
    if protect:protect(path)

def _read_journal(path,bundle,manifest,protect):
    if not path.is_file():raise ValueError("switch journal unavailable")
    _path(path)
    if protect:protect(path)
    if path.stat().st_size>4096:raise ValueError("invalid switch journal")
    actual=json.loads(path.read_text(encoding="utf-8"))
    expected={"format":FORMAT,"bundle_manifest_sha256":digest(bundle/"update-manifest.json"),
              "archive_sha256":manifest["archive_sha256"],"old_version":manifest["old_version"],
              "new_version":manifest["new_version"]}
    if actual!=expected:raise ValueError("switch journal or bundle changed")

def _matches(path,manifest,protect):
    if not path.exists():return False
    try:return _check_manifest_records(path,manifest,protect)
    except (ValueError,OSError,RuntimeError):return False

def _layout(installed,new,old,m,protect):
    old_live=_matches(installed,m["old_program"],protect)
    new_live=_matches(installed,m["candidate"],protect)
    old_saved=_matches(old,m["old_program"],protect)
    new_saved=_matches(new,m["candidate"],protect)
    if old_live and new_saved and not old.exists():return "previous_installed_candidate_staged"
    if not installed.exists() and old_saved and new_saved:return "switch_interrupted"
    if new_live and old_saved and not new.exists():return "candidate_installed"
    # A completed reversal leaves the candidate staged; that layout is
    # intentionally identical to a safely abandoned preparation.
    return "ambiguous"

def inspect(installed,state_root,bundle,ident,program_protect=None,state_protect=None):
    installed,new,old,journal=locations(installed,ident)
    bundle=_path(bundle);m=verify(bundle,state_protect)
    if program_protect:program_protect(installed.parent)
    _read_journal(journal,bundle,m,program_protect)
    layout=_layout(installed,new,old,m,program_protect)
    unchanged=False
    try:state_matches(state_root,m,state_protect);unchanged=True
    except (ValueError,OSError,RuntimeError):pass
    return {"layout":layout,"operational_state_unchanged":unchanged,"archive_sha256":m["archive_sha256"],
            "pre_start_reverse_allowed":unchanged and layout in ("candidate_installed","switch_interrupted"),
            "old_version":m["old_version"],"new_version":m["new_version"],
            "service_start_performed":False,"live_restore_performed":False}

def apply(installed,state_root,bundle,ident,archive,expected_sha256,guard,program_protect=None,state_protect=None,
          rename=None):
    """Switch whole program tree; leave the service disabled and stopped."""
    rename=rename or (lambda a,b:a.rename(b))
    installed,new,old,journal=locations(installed,ident);bundle=_path(bundle)
    if program_protect:program_protect(installed.parent)
    if any(p.exists() or p.is_symlink() for p in (new,old,journal)):
        raise ValueError("existing switch paths; inspect or reconcile before proceeding")
    guard()
    m=verify(bundle,state_protect)
    if not isinstance(expected_sha256,str) or expected_sha256.lower()!=m["archive_sha256"]:
        raise ValueError("trusted candidate digest must match prepared snapshot")
    if _validate_program(installed)!=m["old_version"]:raise ValueError("installed version changed")
    _check_manifest_records(installed,m["old_program"],program_protect)
    state_matches(state_root,m,state_protect)
    # Re-stage trusted ZIP into an admin-protected same-volume sibling.
    # The operational backup's candidate is never executed or installed.
    stage(archive,expected_sha256,new)
    _check_manifest_records(new,m["candidate"],program_protect)
    guard()
    _check_manifest_records(installed,m["old_program"],program_protect)
    state_matches(state_root,m,state_protect)
    _journal(journal,bundle,m,guard,program_protect)
    guard()
    state_matches(state_root,m,state_protect)
    rename(installed,old)
    try:
        guard()
        state_matches(state_root,m,state_protect)
        rename(new,installed)
    except BaseException:
        # Try restoring the original namespace only if no other path
        # occupies it and the original tree is intact. No destructive cleanup.
        if not installed.exists() and _matches(old,m["old_program"],program_protect):
            try:guard();rename(old,installed)
            except Exception:pass
        raise
    guard()
    if _layout(installed,new,old,m,program_protect)!="candidate_installed":
        raise RuntimeError("post-switch verification failed")
    return {"status":"candidate_installed_service_not_started","old_version":m["old_version"],
            "new_version":m["new_version"],"rollback_tree_preserved":True,
            "operational_state_modified":False}

def reverse(installed,state_root,bundle,ident,guard,program_protect=None,state_protect=None,rename=None):
    """Reverse before any operational-state change; do not restore old data over new data."""
    rename=rename or (lambda a,b:a.rename(b))
    installed,new,old,journal=locations(installed,ident);bundle=_path(bundle)
    if program_protect:program_protect(installed.parent)
    guard()
    m=verify(bundle,state_protect);_read_journal(journal,bundle,m,program_protect)
    state_matches(state_root,m,state_protect)
    layout=_layout(installed,new,old,m,program_protect)
    if layout=="previous_installed_candidate_staged":
        return {"status":"previous_program_already_installed","service_start_performed":False}
    if layout=="candidate_installed":
        guard();rename(installed,new)
    elif layout!="switch_interrupted":
        raise RuntimeError("ambiguous switch state; preserve all paths for local recovery")
    guard()
    state_matches(state_root,m,state_protect)
    if installed.exists() or not _matches(old,m["old_program"],program_protect):
        raise RuntimeError("previous program cannot be verified")
    rename(old,installed)
    guard()
    if _layout(installed,new,old,m,program_protect)!="previous_installed_candidate_staged":
        raise RuntimeError("reverse verification failed")
    return {"status":"previous_program_restored_service_not_started","service_start_performed":False,
            "operational_state_modified":False}
